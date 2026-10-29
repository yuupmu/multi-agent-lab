import json
import tempfile
import threading
import unittest

from backend.engine import Conflict, Engine
from test_backend import ROOT, ANSWERS, config, wait_for
from test_openai import FakeProvider


class SequentialProvider(FakeProvider):
    def __init__(self, questions=8):
        super().__init__()
        self.questions = questions

    def invoke(self, envelope, instruction, schema):
        state = envelope['project_state']
        count = len(state['decisions'])
        if envelope['role'] == 'monitor':
            needed = count < self.questions
            result = {'decision': 'human' if needed else 'continue', 'reason': '검토', 'checks': [],
                      'requests': [{'key': 'question_%s' % count, 'question': '조건은?', 'reason': '미정',
                                    'affected_tasks': ['policy'], 'priority': 'normal'}] if needed else []}
        elif envelope['action'] == 'plan':
            result = {'plan': ['구현'], 'assumptions': []}
        else:
            result = {'code': 'def can_cancel(now, booked_at, starts_at):\n    return False\n',
                      'summary': '반영', 'complete': self.questions > 0 and count >= self.questions,
                      'applied_decision_ids': [d['request_id'] for d in state['decisions']], 'checks': []}
        return result, {'response_id': 'fake', 'usage': {}}


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Engine(ROOT, data_dir=self.temp.name)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def start(self, provider):
        self.engine.api_client = provider
        cfg = config(); cfg['mode'] = 'openai'
        return self.engine.start(cfg)

    def test_eight_human_revisions_do_not_hit_six_revision_limit(self):
        state = self.start(SequentialProvider())
        for count in range(8):
            state = wait_for(self.engine, state['id'], lambda s: any(r['key'] == 'question_%s' % count for r in s['requests']))
            request = next(r for r in state['requests'] if r['key'] == 'question_%s' % count)
            self.engine.answer(state['id'], request['id'], {'text': '결정 %s' % count})
        state = wait_for(self.engine, state['id'], lambda s: s['status'] == 'completed')
        self.assertGreater(state['code_version'], 6)
        self.assertEqual(len(state['decisions']), 8)
        self.assertTrue(all(r['status'] == 'applied' for r in state['requests']))

    def test_autonomous_limit_pauses_and_resume_keeps_existing_code(self):
        self.engine.MAX_AUTONOMOUS_STEPS = 2
        state = self.start(SequentialProvider(questions=0))
        state = wait_for(self.engine, state['id'], lambda s: s['status'] == 'paused')
        self.assertEqual(state['code_version'], 2)
        self.engine.resume(state['id'])
        state = wait_for(self.engine, state['id'], lambda s: s['status'] == 'paused')
        self.assertEqual(state['code_version'], 4)
        self.assertEqual(state['execution_epoch'], 1)

    def test_legacy_failure_recovers_answers_and_resumes_after_reconnect(self):
        state = self.start(FakeProvider())
        state = wait_for(self.engine, state['id'], lambda s: len(s['requests']) == 3)
        self.engine.close()
        first = state['requests'][0]
        state = self.engine.answer(state['id'], first['id'], {'text': json.dumps(ANSWERS[first['key']])})
        state.update(status='failed', code_version=6)
        state['events'].append({'message': '작업 오류: 코드 수정 6회에 도달했습니다.'})
        self.engine.store.save(state)
        self.engine = Engine(ROOT, data_dir=self.temp.name)
        recovered = self.engine.get(state['id'])
        self.assertEqual(recovered['status'], 'paused')
        self.assertEqual(recovered['requests'], state['requests'])
        for request in recovered['requests']:
            if request['status'] == 'waiting':
                self.engine.answer(state['id'], request['id'], {'text': json.dumps(ANSWERS[request['key']])})
        decisions = self.engine.get(state['id'])['decisions']
        with self.assertRaises(Conflict):
            self.engine.resume(state['id'])
        self.engine.api_client = FakeProvider()
        self.engine.resume(state['id'])
        result = wait_for(self.engine, state['id'], lambda s: s['status'] == 'completed')
        self.assertEqual(result['decisions'], decisions)
        self.assertGreater(result['code_version'], 6)
        self.assertTrue(all(r['status'] == 'applied' for r in result['requests']))

    def test_old_inflight_error_cannot_fail_resumed_run(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        provider = SequentialProvider()
        invoke = provider.invoke
        def delayed(envelope, *args):
            if envelope['action'] == 'plan' and not entered.is_set():
                entered.set()
                release.wait(5)
                finished.set()
                raise RuntimeError('old request failed')
            return invoke(envelope, *args)
        provider.invoke = delayed
        try:
            state = self.start(provider)
            self.assertTrue(entered.wait(2))
            with self.engine.cv:
                live = self.engine._state(state['id'])
                live['status'] = 'interrupted'
                self.engine._save(live)
            self.engine.resume(state['id'])
            wait_for(self.engine, state['id'], lambda s: len(s['requests']) == 1)
            release.set()
            self.assertTrue(finished.wait(2))
            self.engine.threads[0].join(2)
            self.assertIn(self.engine.get(state['id'])['status'], self.engine.ACTIVE)
        finally:
            release.set()
