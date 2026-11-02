import io
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from backend.diagnostics import validate_monitor, ResponseValidationError, safe_log
from backend.engine import Engine
from backend.openai_client import OpenAIClient, APIError
from test_backend import ROOT, config, wait_for
from test_openai import FAKE_KEY
from test_resume import SequentialProvider


class DiagnosticsTests(unittest.TestCase):
    def test_failed_observer_output_and_exact_limit_are_persisted_without_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            engine=Engine(ROOT,data_dir=temp)
            provider=SequentialProvider(questions=1)
            invoke=provider.invoke
            def too_many(envelope,*args):
                result,meta=invoke(envelope,*args)
                if envelope.get('concept')=='관점 3':
                    result['requests']=[dict(result['requests'][0], key='q%s'%i, reason=FAKE_KEY) for i in range(8)]
                return result,meta
            provider.invoke=too_many
            engine.api_client=provider
            try:
                cfg=config();cfg['mode']='openai'
                run=engine.start(cfg)
                run=wait_for(engine,run['id'],lambda r:r['status']=='paused')
                diagnostic=next(d for d in run['diagnostics'] if d['agent']=='monitor-3')
                self.assertEqual(diagnostic['details'],{'field':'requests.length','expected':'<= 6','actual':8})
                self.assertEqual(diagnostic['stage'],'response_validation')
                self.assertEqual(diagnostic['model'],'gpt-4o-mini')
                call=next(c for c in run['calls'] if c.get('id')==diagnostic['call_id'])
                self.assertEqual(len(call['output']['requests']),8)
                self.assertEqual(call['status'],'failed')
                self.assertIn('started_at',call)
                self.assertIn('finished_at',call)
                self.assertIsInstance(diagnostic['elapsed_ms'],int)
                self.assertTrue(diagnostic['trace'])
                self.assertNotIn(FAKE_KEY,json.dumps(run))
                stored=next(s for s in engine.store.all() if s['id']==run['id'])
                self.assertEqual(stored['diagnostics'][0]['details'],diagnostic['details'])
            finally:
                engine.close()

    def test_bad_requests_type_and_item_path_are_distinguishable(self):
        cases=[({'decision':'human','reason':'x','requests':{},'checks':[]},'requests'),
               ({'decision':'human','reason':'x','requests':[{'key':'BAD KEY'}],'checks':[]},'requests[0].key')]
        for payload,field in cases:
            with self.assertRaises(ResponseValidationError) as caught:validate_monitor(payload)
            self.assertEqual(caught.exception.details['field'],field)

    def test_api_errors_keep_safe_codes_and_incomplete_response_details(self):
        client=OpenAIClient(FAKE_KEY)
        body=json.dumps({'error':{'code':'insufficient_quota','type':'quota_error','param':None,'message':FAKE_KEY}}).encode()
        error=HTTPError('https://api.openai.com',429,'quota',{'x-request-id':'req-test'},io.BytesIO(body))
        with patch('backend.openai_client.build_opener') as opener:
            opener.return_value.open.side_effect=error
            with self.assertRaises(APIError) as caught:client.connect()
        self.assertEqual(caught.exception.details['http_status'],429)
        self.assertEqual(caught.exception.details['code'],'insufficient_quota')
        self.assertEqual(caught.exception.details['request_id'],'req-test')
        self.assertNotIn(FAKE_KEY,str(caught.exception)+json.dumps(caught.exception.details))
        response={'id':'resp-test','status':'incomplete','incomplete_details':{'reason':'max_output_tokens'},'output':[FAKE_KEY]}
        with patch.object(client,'_request',return_value=response):
            with self.assertRaises(APIError) as caught:client.invoke({'model':'gpt-4o','prompt':'x'},'rules',{})
        self.assertEqual(caught.exception.details['incomplete_details']['reason'],'max_output_tokens')
        self.assertEqual(caught.exception.response['id'],'resp-test')
        self.assertNotIn(FAKE_KEY,json.dumps(caught.exception.response))
        self.assertEqual(safe_log({'authorization':'Bearer credential'}),{'authorization':'[REDACTED]'})
