import copy
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from backend.adapters import MockMainAgent
from backend.engine import Engine, Conflict
from backend.isolated import run_cases
from backend.openai_client import OpenAIClient, APIError
from backend.live_schemas import MAIN_RULES, MONITOR_RULES
from test_backend import ROOT, config, ANSWERS, wait_for

FAKE_KEY = 'sk-test_only_not_a_real_key_12345'


class FakeProvider:
    def __init__(self, key=None):
        self.models = ['mock-main', 'mock-monitor']
        self._key = key
        self.envelopes = []
    def connect(self):
        return self.models
    def clear(self):
        self._key = None
    def invoke(self, envelope, instruction, schema):
        self.envelopes.append(copy.deepcopy(envelope))
        state = envelope['project_state']
        policies = {d['key']: json.loads(d['answer']['text']) for d in state['decisions']}
        if envelope['role'] == 'monitor':
            i = int(envelope['concept'].split()[-1])
            key = {1:'cutoff', 2:'calendar', 3:'exceptions'}[i]
            needed = key not in policies
            result = {'decision':'human' if needed else 'continue', 'reason':'입력 정책 확인', 'checks':[],
                      'requests':[{'key':key,'question':key+' 정책은?', 'reason':'누락',
                                   'affected_tasks':['policy'], 'priority':'normal'}] if needed else []}
        elif envelope['action'] == 'plan':
            result = {'plan':['입력 처리','정책 구현'], 'assumptions':['운영 정책 미정']}
        else:
            if len(policies) == 3:
                source = MockMainAgent().invoke({'action':'implement', 'project_state':{'policies':policies}})['code']
            else:
                source = 'def can_cancel(now, booked_at, starts_at):\n    return False\n'
            result = {'code':source, 'summary':'변경', 'complete':len(policies)==3,
                      'applied_decision_ids':[d['request_id'] for d in state['decisions']],
                      'checks':[{'now':'bad','booked_at':'bad','starts_at':'bad','expected':False}]}
        return result, {'response_id':'test-response','usage':{'total_tokens':1}}


class TransportTests(unittest.TestCase):
    def test_model_list_includes_legacy_future_and_finetuned_ids(self):
        client = OpenAIClient(FAKE_KEY)
        models = ['gpt-6-luna', 'gpt-4o', 'gpt-4o-mini', 'future-model', 'ft:' + 'x' * 100]
        with patch.object(client, '_request', return_value={'data':
                [{'id': m} for m in models + models] + [None, {}, {'id': None}]}):
            self.assertEqual(client.connect(), sorted(models))

    def test_responses_payload_preserves_prompt_and_handles_output(self):
        client = OpenAIClient(FAKE_KEY)
        response = {'status':'completed','id':'r1','usage':{},'output':[{'type':'message','content':[{'type':'output_text','text':'{"answer":true}'}]}]}
        envelope={'model':'model-id','prompt':'  그대로\n  ','project_state':{'code_version':1},'role':'monitor'}
        with patch.object(client, '_request', return_value=response) as request:
            result,_ = client.invoke(envelope,'format rules',{'type':'object'})
        body=request.call_args.args[1]
        self.assertEqual(body['input'][1]['content'],envelope['prompt'])
        self.assertFalse(body['store'])
        self.assertEqual(body['text']['format']['type'],'json_schema')
        self.assertNotIn(FAKE_KEY,json.dumps(body))
        self.assertEqual(result,{'answer':True})

    def test_provider_errors_do_not_echo_secret_or_response_body(self):
        client=OpenAIClient(FAKE_KEY)
        error=HTTPError('https://api.openai.com',401,'bad',{},io.BytesIO(FAKE_KEY.encode()))
        with patch('backend.openai_client.build_opener') as opener:
            opener.return_value.open.side_effect=error
            with self.assertRaises(APIError) as caught:
                client.connect()
        self.assertNotIn(FAKE_KEY,str(caught.exception))
        self.assertIn('401',str(caught.exception))

    def test_refusal_and_incomplete_are_not_success(self):
        client=OpenAIClient(FAKE_KEY)
        for response in [{'status':'incomplete'}, {'status':'completed','output':[{'content':[{'type':'refusal','refusal':'no'}]}]}]:
            with patch.object(client,'_request',return_value=response):
                with self.assertRaises(APIError):
                    client.invoke({'model':'x','prompt':'x'},'rules',{})


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.engine=Engine(ROOT,data_dir=self.temp.name)
        self.provider=FakeProvider()
        self.engine.api_client=self.provider
    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()
    def start(self):
        cfg=config(); cfg['mode']='openai'
        return self.engine.start(cfg)

    def test_no_policy_is_seeded_into_initial_main_or_monitor_inputs(self):
        state = self.start()
        state = wait_for(self.engine, state['id'], lambda s: len(s['requests']) == 3)
        first = {}
        for call in state['calls']:
            first.setdefault(call['agent'], call['input'])
        self.assertEqual(set(first), {'main', 'monitor-1', 'monitor-2', 'monitor-3'})
        for envelope in first.values():
            context = envelope['project_state']
            self.assertEqual(context['decisions'], [])
            self.assertEqual(context['policies'], {})
            self.assertNotIn('closed_weekdays', context)
            combined = json.dumps(envelope, ensure_ascii=False) + MAIN_RULES + MONITOR_RULES
            for hidden in ['금요일', '금·토', 'UTC+09', '24시간', '10분', '18:00', '2026-11-10', '2026-12-24', '잘못된 날짜 문자열이나 시간대가 없는 입력은']:
                self.assertNotIn(hidden, combined)
        self.engine.stop(state['id'])

    def test_old_run_decisions_do_not_seed_new_run(self):
        state = self.start()
        state = wait_for(self.engine, state['id'], lambda s: len(s['requests']) == 3)
        request = state['requests'][0]
        self.engine.answer(state['id'], request['id'], {'text': json.dumps(ANSWERS[request['key']])})
        self.engine.stop(state['id'])
        fresh = self.start()
        fresh = wait_for(self.engine, fresh['id'], lambda s: len(s['requests']) == 3)
        self.assertEqual(fresh['decisions'], [])
        for call in fresh['calls']:
            self.assertEqual(call['input']['project_state']['decisions'], [])
        self.engine.stop(fresh['id'])

    def test_real_workflow_path_questions_answers_manual_isolated_grading(self):
        state=self.start()
        state=wait_for(self.engine,state['id'],lambda s:len(s['requests'])==3)
        for request in state['requests']:
            self.engine.answer(state['id'],request['id'],{'text':json.dumps(ANSWERS[request['key']])},'자유 조언')
        state=wait_for(self.engine,state['id'],lambda s:s['status']=='completed',timeout=15)
        self.assertEqual(state['test']['status'],'not_run')
        self.assertTrue(state['verification']['passed'])
        self.assertEqual(state['verification']['total'],1)
        self.assertTrue(all(r['status']=='applied' for r in state['requests']))
        self.assertTrue(all('task' in c['input']['project_state'] for c in state['calls']))
        for call in state['calls']:
            expected=config()['mainAgent']['prompt'] if call['agent']=='main' else config()['monitors'][int(call['agent'][-1])-1]['instructions']
            self.assertEqual(call['input']['prompt'],expected)
            self.assertNotIn('answer_key',json.dumps(call['input']))
        self.engine.test(state['id'])
        state=wait_for(self.engine,state['id'],lambda s:s['test']['status']!='running',timeout=15)
        self.assertEqual(state['test']['status'],'completed',state['test'])
        self.assertEqual(state['test']['report']['score'],100)

    def test_stop_remains_responsive_during_main_api_call(self):
        entered, release=threading.Event(),threading.Event()
        invoke=self.provider.invoke
        def delayed(*args):
            entered.set(); release.wait(5)
            return invoke(*args)
        self.provider.invoke=delayed
        try:
            state=self.start()
            self.assertTrue(entered.wait(2))
            state=self.engine.stop(state['id'])
            self.assertEqual(state['status'],'stopped')
            self.assertIsNone(state['artifact'])
        finally:
            release.set()

    def test_connection_key_never_persisted_and_disconnect_clears_memory(self):
        self.engine.api_client=None
        with patch('backend.engine.OpenAIClient',FakeProvider):
            connection=self.engine.connect(FAKE_KEY)
        self.assertTrue(connection['connected'])
        self.assertNotIn(FAKE_KEY,json.dumps(connection))
        self.assertNotIn(FAKE_KEY.encode(),(Path(self.temp.name)/'runs.sqlite3').read_bytes())
        client=self.engine.api_client
        self.engine.disconnect()
        self.assertIsNone(client._key)
        with self.assertRaises(Conflict):
            self.start()

    def test_sandbox_blocks_private_file_network_and_environment(self):
        protected=Path(self.temp.name)/'private-value.txt'
        protected.write_text('not-readable')
        code='''import os, socket
+def can_cancel(now, booked_at, starts_at):
+    try:
+        open(%r).read()
+        return False
+    except PermissionError:
+        pass
+    try:
+        socket.socket().connect(('127.0.0.1', 9))
+        return False
+    except PermissionError:
+        pass
+    return 'OPENAI_API_KEY' not in os.environ
+'''.replace('\n+','\n') % str(protected)
        with patch.dict('os.environ',{'OPENAI_API_KEY':FAKE_KEY}):
            rows=run_cases(code,[{'now':'a','booked_at':'b','starts_at':'c'}])
        self.assertEqual(rows,[{'actual':True,'error':None}])


if __name__=='__main__':
    unittest.main()
