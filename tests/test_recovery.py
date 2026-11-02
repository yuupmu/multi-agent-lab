import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.engine import Engine, Conflict
from backend.isolated import run_cases
from test_backend import ROOT, config, wait_for
from test_resume import SequentialProvider

CHECK = {'now': 'bad', 'booked_at': 'bad', 'starts_at': 'bad', 'expected': False}
VALID = 'def can_cancel(now, booked_at, starts_at):\n    return False\n'


class RecoveryTests(unittest.TestCase):
    def test_import_failure_returns_diagnostic_instead_of_killing_checker(self):
        rows = run_cases('import pytz\n' + VALID, [CHECK])
        self.assertIsNone(rows[0]['actual'])
        self.assertIn('ModuleNotFoundError (pytz)', rows[0]['error'])

    def test_code_errors_keep_questions_answerable_and_feed_next_revision(self):
        for bad in ['import pytz\n' + VALID, 'def can_cancel(:\n', 'def wrong_name():\n    return False\n']:
            with self.subTest(code=bad), tempfile.TemporaryDirectory() as temp:
                engine = Engine(ROOT, data_dir=temp)
                provider = SequentialProvider(questions=1)
                invoke = provider.invoke
                observed = []
                def broken_first(envelope, *args):
                    result, meta = invoke(envelope, *args)
                    if envelope['action'] == 'implement':
                        if not envelope['project_state']['decisions']:
                            result.update(code=bad, checks=[CHECK])
                        else:
                            observed.append(envelope['project_state']['verification'])
                    return result, meta
                provider.invoke = broken_first
                engine.api_client = provider
                try:
                    cfg = config(); cfg['mode'] = 'openai'
                    run = engine.start(cfg)
                    run = wait_for(engine, run['id'], lambda r: r['code_version'] >= 1 and bool(r['requests']))
                    self.assertFalse(run['verification']['passed'])
                    self.assertIn(run['status'], engine.ACTIVE)
                    engine.answer(run['id'], run['requests'][0]['id'], {'text': '정책 결정'})
                    done = wait_for(engine, run['id'], lambda r: r['status'] == 'completed')
                    self.assertTrue(done['verification']['passed'])
                    self.assertFalse(observed[0]['passed'])
                finally:
                    engine.close()

    def test_failed_run_accepts_answer_without_key_and_resumes_with_context(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = Engine(ROOT, data_dir=temp)
            try:
                provider = SequentialProvider(questions=1)
                engine.api_client = provider
                cfg = config(); cfg['mode'] = 'openai'
                run = engine.start(cfg)
                run = wait_for(engine, run['id'], lambda r: r['code_version'] >= 1 and bool(r['requests']))
                with engine.cv:
                    state = engine._state(run['id'])
                    state.update(status='failed', phase='실행 실패')
                    engine._event(state, '작업 오류: 이전 검증 실패')
                    engine._save(state)
                engine.disconnect()
                engine.answer(run['id'], run['requests'][0]['id'], {'text': '보존할 답변'})
                with self.assertRaises(Conflict): engine.resume(run['id'])
                engine.api_client = SequentialProvider(questions=1)
                engine.resume(run['id'])
                done = wait_for(engine, run['id'], lambda r: r['status'] == 'completed')
                self.assertEqual(done['decisions'][0]['answer']['text'], '보존할 답변')
                last = [c for c in done['calls'] if c['agent'] == 'main'][-1]
                self.assertIn('이전 검증 실패', last['input']['project_state']['last_error'])
            finally:
                engine.close()

    def test_main_api_and_monitor_errors_pause_instead_of_locking_answers(self):
        for failure_role in ['main', 'monitor']:
            with self.subTest(role=failure_role), tempfile.TemporaryDirectory() as temp:
                engine = Engine(ROOT, data_dir=temp)
                provider = SequentialProvider(questions=1)
                invoke = provider.invoke
                question_seen = threading.Event()
                def failing(envelope, *args):
                    if envelope['role'] == 'monitor' and envelope['concept'] == '관점 1':
                        result = invoke(envelope, *args)
                        question_seen.set()
                        return result
                    if (failure_role == 'main' and envelope['action'] == 'implement') or (failure_role == 'monitor' and envelope.get('concept') == '관점 3'):
                        question_seen.wait(2)
                        wait_for(engine, run_id[0], lambda r: bool(r['requests']))
                        raise RuntimeError('simulated API failure')
                    return invoke(envelope, *args)
                run_id = []
                # Assign ID before releasing worker calls.
                ready = threading.Event()
                def gated(*args):
                    ready.wait(2)
                    return failing(*args)
                provider.invoke = gated
                engine.api_client = provider
                try:
                    cfg=config(); cfg['mode']='openai'
                    run=engine.start(cfg); run_id.append(run['id']); ready.set()
                    run=wait_for(engine,run['id'],lambda r:r['status']=='paused')
                    self.assertEqual(run['pause_reason'],'execution_error')
                    engine.answer(run['id'],run['requests'][0]['id'],{'text':'오류 후에도 답변'})
                    self.assertEqual(len(engine.get(run['id'])['decisions']),1)
                finally:
                    ready.set()
                    engine.close()

    def test_report_omission_retries_with_feedback_but_never_fabricates_application(self):
        for always_omit in [False, True]:
            with self.subTest(always_omit=always_omit), tempfile.TemporaryDirectory() as temp:
                engine = Engine(ROOT, data_dir=temp)
                provider = SequentialProvider(questions=1)
                invoke = provider.invoke
                attempts = []
                def missing_report(envelope, *args):
                    result, meta = invoke(envelope, *args)
                    if envelope['action'] == 'implement' and envelope['project_state']['decisions']:
                        attempts.append(envelope['project_state'])
                        if always_omit or len(attempts) == 1:
                            result['applied_decision_ids'] = []
                    return result, meta
                provider.invoke = missing_report
                engine.api_client = provider
                try:
                    cfg=config();cfg['mode']='openai'
                    run=engine.start(cfg)
                    run=wait_for(engine,run['id'],lambda r:r['code_version'] >= 1 and bool(r['requests']))
                    question=run['requests'][0]
                    engine.answer(run['id'],question['id'],{'text':'사용자 결정'})
                    expected_status='paused' if always_omit else 'completed'
                    run=wait_for(engine,run['id'],lambda r:r['status']==expected_status)
                    self.assertEqual(len(attempts),3 if always_omit else 2)
                    self.assertEqual(attempts[1]['response_feedback']['missing_ids'],[question['id']])
                    self.assertEqual(attempts[1]['required_decision_ids'],[question['id']])
                    self.assertEqual(run['requests'][0]['status'],'answered' if always_omit else 'applied')
                    if always_omit:
                        self.assertEqual(run['code_version'],1)
                        self.assertNotIn('applied_version',run['requests'][0])
                finally:
                    engine.close()

    def test_context_only_answer_is_acknowledged_without_claiming_code_application(self):
        with tempfile.TemporaryDirectory() as temp:
            engine=Engine(ROOT,data_dir=temp)
            provider=SequentialProvider(questions=1)
            invoke=provider.invoke
            def context_report(envelope,*args):
                result,meta=invoke(envelope,*args)
                if envelope['action']=='implement':
                    result['context_decision_ids']=result['applied_decision_ids']
                    result['applied_decision_ids']=[]
                return result,meta
            provider.invoke=context_report
            engine.api_client=provider
            try:
                cfg=config();cfg['mode']='openai'
                run=engine.start(cfg)
                run=wait_for(engine,run['id'],lambda r:r['code_version'] >= 1 and bool(r['requests']))
                engine.answer(run['id'],run['requests'][0]['id'],{'text':'아직 계획 단계입니다.'})
                run=wait_for(engine,run['id'],lambda r:r['status']=='completed')
                self.assertEqual(run['requests'][0]['status'],'acknowledged')
                self.assertNotIn('applied_version',run['requests'][0])
                self.assertIn('acknowledged_version',run['requests'][0])
            finally:
                engine.close()
