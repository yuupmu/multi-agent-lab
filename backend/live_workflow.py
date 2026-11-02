import ast
import copy
import hashlib
import uuid
from datetime import datetime

from .adapters import agent_input
from .isolated import check_code
from .live_schemas import PLAN, schemas_for, main_rules, monitor_rules
from .tasks import TASKS, task_id
from .diagnostics import timestamp, safe_log, validate_monitor, error_record, ResponseValidationError


class RunStopped(Exception):
    pass


class LiveWorkflow:
    MAX_AUTONOMOUS_STEPS = 6
    MAX_REPORT_RETRIES = 2

    def _public_verification(self, code, checks, selected_task):
        # Candidate mistakes become feedback for revision, not fatal workflow errors.
        try:
            definition = TASKS[selected_task]
            tree = ast.parse(code)
            if not any(isinstance(n, ast.FunctionDef) and n.name == definition['function'] and len(n.args.args) == len(definition['arguments']) for n in tree.body):
                raise ValueError('%s(%s) 함수가 필요합니다.' % (definition['function'], ', '.join(definition['arguments'])))
            return check_code(code, checks, selected_task)
        except (SyntaxError, ValueError, RuntimeError) as exc:
            message = ('Python 문법 오류 (줄 %s)' % exc.lineno) if isinstance(exc, SyntaxError) else str(exc)[:1000]
            return {'passed': False, 'correct': 0, 'total': len(checks) if isinstance(checks, list) else 0,
                    'results': [], 'error': message}

    def _record_call_error(self, state, call, exc, ignored=False):
        if call and call.get('error'):
            return
        if call:
            if getattr(exc, 'response', None) is not None:
                call['raw_response'] = safe_log(exc.response)
                call['response_id'] = exc.response.get('id')
                call['stage'] = 'response_decoding'
            self._finish_call(call)
        record = error_record(exc, call, state['phase'], state['code_version'], state.get('execution_epoch', 0))
        record['ignored'] = ignored
        if call:
            record['execution_epoch'] = call.get('execution_epoch', record['execution_epoch'])
            record['code_version'] = call.get('input', {}).get('project_state', {}).get('code_version', record['code_version'])
        state.setdefault('diagnostics', []).append(record)
        if call:
            call.update(status='failed', error=record)

    def _finish_call(self, call):
        call['finished_at'] = timestamp()
        if call.get('started_at'):
            call['elapsed_ms'] = round((datetime.fromisoformat(call['finished_at']) - datetime.fromisoformat(call['started_at'])).total_seconds() * 1000)

    def _capture_response(self, state, call, result, metadata, epoch):
        # Persist before validation, including responses arriving after a pause.
        call.update(output=safe_log(result), **safe_log(metadata))
        call['response_received_at'] = timestamp()
        call['stage'] = 'response_validation'
        self._finish_call(call)
        if not self._live_active(state, epoch):
            call['status'] = 'discarded'
        self._save(state)

    def _pause_live_error(self, state, message):
        message = safe_log(message)
        state.update(status='paused', phase='오류 · 답변 입력/계속하기 대기',
                     pause_reason='execution_error', last_error=message)
        self._event(state, message)
        self._save(state)

    def _live_active(self, state, epoch):
        return state['status'] in self.ACTIVE and state.get('execution_epoch', 0) == epoch

    def _live_context(self, state):
        context = self._context(state)
        benchmark = self._benchmark_path(state)
        public_task = (benchmark / 'agent/TASK.md').read_text()
        if task_id(state) == 'allocation':
            public_task += '\n' + (benchmark / 'agent/OUTPUT_FORMAT.md').read_text()
        context.update(task=public_task,
                       assumptions=state.get('assumptions', []), requests=state['requests'],
                       monitor_feedback=state['monitors'], verification=state['verification'],
                       last_error=state.get('last_error'), rejected_code=state.get('rejected_code'),
                       required_decision_ids=[d['request_id'] for d in state['decisions']],
                       response_feedback=state.get('response_feedback'))
        return context

    def _live_implement(self, run_id, selected_task, epoch):
        for attempt in range(self.MAX_REPORT_RETRIES + 1):
            result, envelope = self._live_call(run_id, 'implement', schemas_for(selected_task)[0], epoch)
            expected = {d['request_id'] for d in envelope['project_state']['decisions']}
            reported = result.get('applied_decision_ids')
            context_ids = result.get('context_decision_ids', [])
            valid = all(isinstance(ids, list) and all(isinstance(x, str) for x in ids) for ids in [reported, context_ids])
            actual = set(reported) | set(context_ids) if valid else set()
            disjoint = valid and not (set(reported) & set(context_ids))
            with self.cv:
                state = self._state(run_id)
                if not self._live_active(state, epoch):
                    raise RunStopped()
                if disjoint and actual == expected:
                    state.pop('response_feedback', None)
                    state.pop('rejected_code', None)
                    if attempt:
                        self._event(state, '답변 반영 보고 재요청 완료. 코드 검증을 진행합니다.')
                    self._save(state)
                    return result, envelope
                state['response_feedback'] = {
                    'error': '답변 처리 보고가 누락되거나 중복됐습니다. 코드 요구사항은 실제 반영 후 applied_decision_ids에, 코드 변경이 불필요한 맥락 답변은 context_decision_ids에 넣으세요. required_decision_ids의 각 ID를 정확히 한 목록에 포함하고 전체 소스를 다시 반환하세요. ID만 임의로 추가하지 마세요.',
                    'required_decision_ids': sorted(expected),
                    'missing_ids': sorted(expected - actual), 'unknown_ids': sorted(actual - expected),
                }
                state['rejected_code'] = result.get('code')
                self._event(state, '답변 반영 보고 불일치 · 누락 %s건, 알 수 없는 ID %s건 · %s' %
                            (len(expected - actual), len(actual - expected),
                             '자동 재요청 %s/%s' % (attempt + 1, self.MAX_REPORT_RETRIES) if attempt < self.MAX_REPORT_RETRIES else '재요청 한도 도달'))
                self._save(state)
            if attempt == self.MAX_REPORT_RETRIES:
                raise ResponseValidationError('decision_ids', sorted(expected), {'applied':reported,'context':context_ids}, '답변 반영 보고가 자동 재요청 2회 후에도 일치하지 않습니다. 답변은 보존됐으며, 계속하기로 재시도할 수 있습니다.')

    def _live_call(self, run_id, action, schema, epoch):
        with self.cv:
            state = self._state(run_id)
            if not self._live_active(state, epoch):
                raise RunStopped()
            envelope = agent_input('main', state['config']['mainAgent'], self._live_context(state), action)
            call = {'id':uuid.uuid4().hex, 'agent': 'main', 'input': envelope, 'status': 'running',
                    'started_at':timestamp(), 'stage':'api_request', 'execution_epoch':epoch}
            state['calls'].append(call)
            client = self.api_client
            self._save(state)
        try:
            result, metadata = client.invoke(envelope, main_rules(task_id(state)), schema)
        except Exception as exc:
            with self.cv:
                self._record_call_error(state, call, exc, ignored=not self._live_active(state, epoch))
                self._save(state)
            raise
        with self.cv:
            self._capture_response(state, call, result, metadata, epoch)
            if not self._live_active(state, epoch):
                raise RunStopped()
            call.update(status='completed')
            self._save(state)
        return result, envelope

    def _live_observe(self, state):
        # Called while holding cv; all three receive the same immutable snapshot.
        snapshot = copy.deepcopy(self._live_context(state))
        for config in state['config']['monitors']:
            envelope = agent_input('monitor', config, snapshot, 'observe')
            call = {'id':uuid.uuid4().hex, 'agent': 'monitor-%s' % config['id'], 'input': envelope, 'status': 'running',
                    'started_at':timestamp(), 'stage':'api_request', 'execution_epoch':state.get('execution_epoch', 0)}
            state['calls'].append(call)
            state['monitors'][config['id'] - 1]['status'] = 'observing'
            self.pool.submit(self._live_monitor, state['id'], config['id'], envelope, call, self.api_client, state.get('execution_epoch', 0))

    def _live_monitor(self, run_id, monitor_id, envelope, call, client, epoch):
        try:
            with self.cv:
                if not self._live_active(self._state(run_id), epoch):
                    return
                selected_task = task_id(self._state(run_id))
            result, metadata = client.invoke(envelope, monitor_rules(selected_task), schemas_for(selected_task)[1])
            with self.cv:
                state = self._state(run_id)
                self._capture_response(state, call, result, metadata, epoch)
                if not self._live_active(state, epoch):
                    return
            validate_monitor(result)
            decision = result.get('decision')
            items = result.get('requests')
            checks = result.get('checks')
            code = envelope['project_state']['code']
            verification = self._public_verification(code, checks, selected_task) if checks and code else None
            with self.cv:
                state = self._state(run_id)
                if not self._live_active(state, epoch):
                    return
                call.update(status='completed')
                monitor = state['monitors'][monitor_id - 1]
                version = envelope['project_state']['code_version']
                if monitor['version'] is not None and version < monitor['version']:
                    self._save(state)
                    return
                monitor.update(status='observed', decision=decision, version=version,
                               reason=result['reason'], verification=verification)
                for item in items:
                    existing = next((r for r in state['requests'] if r['key'] == item['key']), None)
                    if existing:
                        if monitor_id not in existing['sources']:
                            existing['sources'].append(monitor_id)
                        continue
                    state['requests'].append({**item, 'id': uuid.uuid4().hex, 'sources': [monitor_id],
                                              'status': 'waiting', 'answer': None, 'observed_version': version,
                                              'fields': [{'name': 'text', 'type': 'text', 'label': '답변'}]})
                    self._event(state, item['question'], 'monitor-%s' % monitor_id)
                self._live_task_status(state)
                self._save(state)
        except Exception as exc:
            with self.cv:
                state = self._state(run_id)
                self._record_call_error(state, call, exc, ignored=not self._live_active(state, epoch))
                if self._live_active(state, epoch):
                    state['monitors'][monitor_id - 1].update(status='error', reason=safe_log(str(exc)))
                    self._pause_live_error(state, '감시 %s 오류: %s' % (monitor_id, str(exc)))
                else:
                    self._save(state)

    def _live_task_status(self, state):
        affected = {task for r in state['requests'] if r['status'] in ('waiting', 'answered', 'applying') for task in r['affected_tasks']}
        for task in state['tasks']:
            task['status'] = 'blocked' if task['id'] in affected else ('completed' if state['code_version'] else 'pending')

    def _work_live(self, run_id, resume=False, epoch=0):
        try:
            with self.cv:
                state = self._state(run_id)
                selected_task = task_id(state)
                definition = TASKS[selected_task]
                needs_plan = not resume or not state['plan']
            if needs_plan:
                plan, _ = self._live_call(run_id, 'plan', PLAN, epoch)
                if not isinstance(plan.get('plan'), list) or not all(isinstance(x, str) for x in plan['plan']) or not isinstance(plan.get('assumptions'), list):
                    raise ValueError('계획 응답 형식 오류')
            with self.cv:
                if not self._live_active(state, epoch):
                    return
                if needs_plan:
                    state['plan'], state['assumptions'] = plan['plan'], plan['assumptions']
                self._live_observe(state)
                self._save(state)
            autonomous_steps = 0
            while True:
                with self.cv:
                    if not self._live_active(state, epoch):
                        return
                    # Human feedback starts a fresh work cycle; it is never charged
                    # against the consecutive autonomous-revision budget.
                    if any(r['status'] == 'answered' for r in state['requests']):
                        autonomous_steps = 0
                    elif autonomous_steps >= self.MAX_AUTONOMOUS_STEPS:
                        state.update(status='paused', phase='자동 수정 한도 · 계속하기 대기', pause_reason='autonomous_limit')
                        self._event(state, '새 인간 답변 없이 자동 수정 %s회 진행했습니다. 답변 입력은 가능하며 계속하기로 이어갈 수 있습니다.' % self.MAX_AUTONOMOUS_STEPS)
                        self._save(state)
                        return
                    else:
                        autonomous_steps += 1
                    state['autonomous_steps'] = autonomous_steps
                    state.update(status='running', phase='코드 작성·수정')
                    self._save(state)
                result, envelope = self._live_implement(run_id, selected_task, epoch)
                code = result.get('code')
                if not isinstance(code, str) or len(code) > 100000:
                    raise ValueError('생성 코드 크기·형식 오류')
                if type(result.get('complete')) is not bool:
                    raise ValueError('완료 판단 형식 오류')
                expected = {d['request_id'] for d in envelope['project_state']['decisions']}
                verification = self._public_verification(code, result.get('checks'), selected_task)
                with self.cv:
                    if not self._live_active(state, epoch):
                        return
                    state['code_version'] += 1
                    version = state['code_version']
                    filename = 'solution_v%s.py' % version
                    (self.data / run_id / filename).write_text(code)
                    state['artifact'] = {'filename': filename, 'code': code, 'sha256': hashlib.sha256(code.encode()).hexdigest()}
                    state['verification'] = {'type': 'syntax_and_public_checks', 'code_version': version, **verification,
                                             'note': '문법·모델 제안 자체 검증. 인간 결정 반영은 모델 보고이며 최종 정확도는 별도 채점.'}
                    for request in state['requests']:
                        if request['id'] in expected and request['status'] == 'answered':
                            if request['id'] in result.get('context_decision_ids', []):
                                request.update(status='acknowledged', acknowledged_version=version)
                            else:
                                request.update(status='applied', applied_version=version)
                    self._event(state, '코드 v%s 작성 · 자체 검증 %s/%s' % (version, verification['correct'], verification['total']), 'main')
                    self._live_task_status(state)
                    self._live_observe(state)
                    state['phase'] = '감시 검토'
                    self._save(state)
                while True:
                    with self.cv:
                        if not self._live_active(state, epoch):
                            return
                        if any(r['status'] == 'answered' for r in state['requests']):
                            break  # Apply early answers immediately, without a monitor barrier.
                        waiting = any(r['status'] == 'waiting' for r in state['requests'])
                        reviewed = all(m['version'] == version and m['status'] == 'observed' for m in state['monitors'])
                        if reviewed and not waiting:
                            checks_pass = all(m.get('verification', {}).get('passed', False) if m.get('verification') else m['decision'] != 'verify' for m in state['monitors'])
                            if result['complete'] and verification['passed'] and checks_pass:
                                state.update(status='completed', phase='프로젝트 완료')
                                for task in state['tasks']:
                                    task['status'] = 'completed'
                                self._event(state, '프로젝트 완료. 테스트하기로 %s문항을 채점할 수 있습니다.' % definition['total'])
                                self._save(state)
                                return
                            break
                        desired = 'waiting' if waiting else 'running'
                        if state['status'] != desired:
                            state.update(status=desired, phase='인간 판단 대기' if waiting else '감시 검토')
                            self._save(state)
                        self.cv.wait(timeout=1)
        except RunStopped:
            return
        except Exception as exc:
            with self.cv:
                state = self._state(run_id)
                if self._live_active(state, epoch):
                    call = next((c for c in reversed(state['calls']) if c['agent'] == 'main' and c.get('execution_epoch', 0) == epoch), None)
                    self._record_call_error(state, call, exc)
                    self._pause_live_error(state, '작업 오류: ' + str(exc))
