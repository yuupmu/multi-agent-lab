import ast
import copy
import hashlib
import re
import uuid

from .adapters import agent_input
from .isolated import check_code
from .live_schemas import PLAN, schemas_for, main_rules, monitor_rules
from .tasks import TASKS, task_id


class RunStopped(Exception):
    pass


class LiveWorkflow:
    MAX_AUTONOMOUS_STEPS = 6

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
                       monitor_feedback=state['monitors'], verification=state['verification'])
        return context

    def _live_call(self, run_id, action, schema, epoch):
        with self.cv:
            state = self._state(run_id)
            if not self._live_active(state, epoch):
                raise RunStopped()
            envelope = agent_input('main', state['config']['mainAgent'], self._live_context(state), action)
            call = {'agent': 'main', 'input': envelope, 'status': 'running'}
            state['calls'].append(call)
            client = self.api_client
            self._save(state)
        result, metadata = client.invoke(envelope, main_rules(task_id(state)), schema)
        with self.cv:
            if not self._live_active(state, epoch):
                raise RunStopped()
            call.update(status='completed', output=result, **metadata)
            self._save(state)
        return result, envelope

    def _live_observe(self, state):
        # Called while holding cv; all three receive the same immutable snapshot.
        snapshot = copy.deepcopy(self._live_context(state))
        for config in state['config']['monitors']:
            envelope = agent_input('monitor', config, snapshot, 'observe')
            call = {'agent': 'monitor-%s' % config['id'], 'input': envelope, 'status': 'running'}
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
            decision = result.get('decision')
            if decision not in ('continue', 'verify', 'human') or not isinstance(result.get('reason'), str):
                raise ValueError('감시 응답 형식 오류')
            items = result.get('requests')
            checks = result.get('checks')
            if not isinstance(items, list) or len(items) > 6 or not isinstance(checks, list):
                raise ValueError('감시 요청 형식 오류')
            if (decision == 'human') != bool(items) or (decision == 'verify' and not checks):
                raise ValueError('감시 판단에 필요한 질문 또는 검증 항목이 없습니다.')
            for item in items:
                if not isinstance(item, dict) or not re.fullmatch('[a-z0-9_]{1,64}', item.get('key', '')):
                    raise ValueError('질문 key 형식 오류')
                if any(not isinstance(item.get(k), str) or not 1 <= len(item[k]) <= 2000 for k in ('question', 'reason')):
                    raise ValueError('질문·근거 형식 오류')
                if not isinstance(item.get('affected_tasks'), list) or not item['affected_tasks'] or not set(item['affected_tasks']) <= {'input', 'policy', 'docs'}:
                    raise ValueError('영향받는 작업 형식 오류')
                if item.get('priority') not in ('normal', 'high'):
                    raise ValueError('우선순위 형식 오류')
            code = envelope['project_state']['code']
            verification = check_code(code, checks, selected_task) if checks and code else None
            with self.cv:
                state = self._state(run_id)
                if not self._live_active(state, epoch):
                    return
                call.update(status='completed', output=result, **metadata)
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
                if self._live_active(state, epoch):
                    call['status'] = 'failed'
                    state['status'] = 'failed'
                    self._event(state, '감시 오류: ' + str(exc))
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
                result, envelope = self._live_call(run_id, 'implement', schemas_for(selected_task)[0], epoch)
                code = result.get('code')
                if not isinstance(code, str) or len(code) > 100000:
                    raise ValueError('생성 코드 크기·형식 오류')
                tree = ast.parse(code)
                if not any(isinstance(n, ast.FunctionDef) and n.name == definition['function'] and len(n.args.args) == len(definition['arguments']) for n in tree.body):
                    raise ValueError('%s(%s) 함수가 필요합니다.' % (definition['function'], ', '.join(definition['arguments'])))
                if type(result.get('complete')) is not bool:
                    raise ValueError('완료 판단 형식 오류')
                expected = {d['request_id'] for d in envelope['project_state']['decisions']}
                if not isinstance(result.get('applied_decision_ids'), list) or set(result['applied_decision_ids']) != expected:
                    raise ValueError('메인 에이전트가 모든 인간 결정의 반영을 보고하지 않았습니다.')
                verification = check_code(code, result.get('checks'), selected_task)
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
                    state.update(status='failed', phase='실행 실패')
                    self._event(state, '작업 오류: ' + str(exc))
                    self._save(state)
