"""Server-only Responses API transport. Credentials live only in memory."""
import json
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.request import Request, HTTPRedirectHandler, build_opener


class APIError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OpenAIClient:
    def __init__(self, key):
        if not isinstance(key, str) or not re.fullmatch(r'sk-[A-Za-z0-9_-]{16,512}', key.strip()):
            raise APIError('API 키 형식을 확인하세요.')
        self._key = key.strip()
        self.models = []

    def clear(self):
        self._key = None
        self.models = []

    def _request(self, path, body=None):
        key = self._key
        if not key:
            raise APIError('API 키를 먼저 연결하세요.')
        req = Request('https://api.openai.com/v1/' + path,
                      data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
                      headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        try:
            with build_opener(NoRedirect()).open(req, timeout=20 if body is None else 120) as response:
                data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise APIError('API 응답이 너무 큽니다.')
            return json.loads(data)
        except HTTPError as exc:
            # Do not forward provider error bodies: they can contain credentials or prompts.
            messages = {400: '모델 ID와 Responses·구조화 출력 지원 여부를 확인하세요.',
                        401: 'API 키가 유효하지 않습니다.', 403: '이 키에 필요한 API 권한이 없습니다.',
                        404: '모델을 찾을 수 없거나 접근 권한이 없습니다.',
                        429: 'API 사용 한도 또는 호출 제한에 도달했습니다.'}
            raise APIError('OpenAI API %s: %s' % (exc.code, messages.get(exc.code, '잠시 후 다시 시도하세요.'))) from None
        except (URLError, TimeoutError, socket.timeout):
            raise APIError('OpenAI 연결 실패 또는 응답 시간 초과입니다.') from None
        except (ValueError, UnicodeError):
            raise APIError('API 응답을 읽을 수 없습니다.') from None

    def connect(self):
        response = self._request('models')
        self.models = sorted({m['id'] for m in response.get('data', [])
                              if isinstance(m, dict) and isinstance(m.get('id'), str)})
        return self.models

    def invoke(self, envelope, instruction, schema):
        response = self._request('responses', {
            'model': envelope['model'], 'store': False, 'max_output_tokens': 10000,
            'input': [{'role': 'developer', 'content': instruction},
                      {'role': 'developer', 'content': envelope['prompt']},
                      {'role': 'user', 'content': json.dumps({k: v for k, v in envelope.items() if k != 'prompt'}, ensure_ascii=False)}],
            'text': {'format': {'type': 'json_schema', 'name': 'agent_result', 'strict': True, 'schema': schema}}})
        if response.get('status') != 'completed':
            raise APIError('모델 응답이 완료되지 않았습니다. 출력 한도 또는 모델 설정을 확인하세요.')
        chunks = []
        for item in response.get('output', []):
            for content in item.get('content', []):
                if content.get('type') == 'refusal':
                    raise APIError('모델이 요청을 거절했습니다.')
                if content.get('type') == 'output_text':
                    chunks.append(content.get('text', ''))
        try:
            result = json.loads(''.join(chunks))
            if not isinstance(result, dict):
                raise ValueError()
        except (ValueError, TypeError):
            raise APIError('모델이 올바른 JSON을 반환하지 않았습니다.') from None
        return result, {'response_id': response.get('id'), 'usage': response.get('usage')}
