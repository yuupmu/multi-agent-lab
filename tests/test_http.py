import json
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from backend.server import create_server
from test_backend import ANSWERS, ROOT, config, wait_for
from test_openai import FakeProvider, FAKE_KEY


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server, self.engine = create_server(ROOT, port=0, data_dir=self.temp.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%s" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.engine.close()
        self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        defaults = {"Content-Type": "application/json"}
        defaults.update(headers or {})
        req = Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=defaults)
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            data = response.read()
            return response.status, json.loads(data) if response.headers.get_content_type() == "application/json" else data.decode()

    def test_full_http_flow(self):
        self.assertEqual(self.request("/api/health"), (200, {"mode": "mock", "model_connected": False}))
        code, page = self.request("/")
        self.assertEqual(code, 200)
        self.assertEqual(page.count("전달받은 프로젝트 상태를 담당 관점으로 검토하고"), 3)
        code, state = self.request("/api/runs", config())
        self.assertEqual(code, 201)
        base = "/api/runs/" + state["id"]
        self.assertEqual(self.request(base + "/test", {})[0], 409)
        state = wait_for(self.engine, state["id"], lambda s: len(s["requests"]) == 3)
        for request in state["requests"]:
            self.assertEqual(self.request(base + "/requests/" + request["id"] + "/answer",
                                          {"answer": ANSWERS[request["key"]], "note": "HTTP 입력"})[0], 200)
        wait_for(self.engine, state["id"], lambda s: s["status"] == "completed")
        self.assertEqual(self.request(base + "/test", {})[0], 202)
        wait_for(self.engine, state["id"], lambda s: s["test"]["status"] != "running")
        result = self.request("/api/runs/latest")[1]
        self.assertEqual(result["test"]["report"]["score"], 100)
        self.assertEqual(self.request(base + "/artifact")[1]["sha256"], result["artifact"]["sha256"])

    def test_request_validation_and_private_files_not_served(self):
        for path in ["/backend/engine.py", "/reservation_benchmark/evaluator/answer_key.json", "/var/runs.sqlite3", "/../README.md"]:
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request("/api/health", headers={"Host": "example.com"})[0], 403)
        self.assertEqual(self.request("/api/runs", config(), {"Origin": "https://example.com"})[0], 403)
        self.assertEqual(self.request("/api/runs", {})[0], 400)
        self.assertEqual(self.request("/api/runs", config(), {"Content-Type": "text/plain"})[0], 415)

    def test_allocation_selection_rules_and_scoring_routes(self):
        from test_allocation import AllocationProvider
        self.engine.api_client = AllocationProvider()
        cfg = config(); cfg.update(mode='openai',task_id='allocation')
        status, rules = self.request('/api/tasks/allocation/rules')
        self.assertEqual(status,200)
        self.assertIn('진행자 전용',rules['text'])
        self.assertEqual(self.request('/resource_allocation_benchmark/evaluator/answer_key.json')[0],404)
        status, state = self.request('/api/runs', cfg)
        self.assertEqual(status,201)
        base = '/api/runs/'+state['id']
        state = wait_for(self.engine,state['id'],lambda s:len(s['requests'])==1)
        self.assertEqual(self.request(base+'/requests/'+state['requests'][0]['id']+'/answer', {'answer':{'text':'테스트 정책'}})[0],200)
        wait_for(self.engine,state['id'],lambda s:s['status']=='completed')
        self.assertEqual(self.request(base+'/test',{})[0],202)
        result = wait_for(self.engine,state['id'],lambda s:s['test']['status']!='running',timeout=20)
        self.assertEqual(result['test']['report']['total'],150)
        self.assertEqual(result['test']['report']['score'],150)

    def test_connection_endpoints_and_rejection_before_key(self):
        self.assertEqual(self.request('/api/connection/refresh', {})[0], 409)
        cfg = config(); cfg['mode'] = 'openai'
        self.assertEqual(self.request('/api/runs', cfg)[0], 409)
        self.assertEqual(self.request('/api/connection', {'key': 'invalid'})[0], 400)
        with patch('backend.engine.OpenAIClient', FakeProvider):
            status, connection = self.request('/api/connection', {'key': FAKE_KEY})
        self.assertEqual(status, 200)
        self.assertTrue(connection['connected'])
        self.assertNotIn(FAKE_KEY, json.dumps(connection))
        self.assertTrue(self.request('/api/health')[1]['model_connected'])
        provider = self.engine.api_client
        provider.models += ['gpt-6-luna', 'gpt-4o']
        status, refreshed = self.request('/api/connection/refresh', {})
        self.assertEqual(status, 200)
        self.assertIn('gpt-4o', refreshed['models'])
        self.assertIs(self.engine.api_client, provider)
        self.assertNotIn(FAKE_KEY, json.dumps(refreshed))
        with patch.object(provider, 'connect', side_effect=ValueError('refresh failed')):
            self.assertEqual(self.request('/api/connection/refresh', {})[0], 400)
        self.assertFalse(self.engine.connecting)
        self.assertTrue(self.request('/api/connection')[1]['connected'])
        _, state = self.request('/api/runs', config())
        wait_for(self.engine, state['id'], lambda s: len(s['requests']) == 3)
        self.assertEqual(self.request('/api/connection/refresh', {})[0], 409)
        self.engine.stop(state['id'])
        self.assertFalse(self.request('/api/connection/disconnect', {})[1]['connected'])


if __name__ == "__main__":
    unittest.main()
