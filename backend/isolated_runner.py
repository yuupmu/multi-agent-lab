"""Trusted child process; receives cases only, never keys or benchmark answers."""
import contextlib
import importlib.util
import json
import resource
import re
import sys

resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))

class Sink:
    def write(self, text):
        return len(text)
    def flush(self):
        pass

payload = json.loads(sys.stdin.read())
cases, task = payload['cases'], payload['task_id']
with contextlib.redirect_stdout(Sink()), contextlib.redirect_stderr(Sink()):
    spec = importlib.util.spec_from_file_location('submission', sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    rows = []
    try:
        spec.loader.exec_module(module)
        load_error = None
    except Exception as exc:
        load_error = '코드 불러오기 실패: ' + type(exc).__name__
        if isinstance(exc, ModuleNotFoundError) and re.fullmatch(r'[A-Za-z0-9_.]{1,80}', exc.name or ''):
            load_error += ' (' + exc.name + '). 외부 패키지 대신 표준 라이브러리를 사용하세요.'
        rows = [{'actual': None, 'error': load_error} for _ in cases]
    for case in ([] if load_error else cases):
        try:
            if task == 'allocation':
                result = module.simulate(case['initial_state'], case['events'])
                if type(result) is not dict:
                    raise TypeError()
            else:
                result = module.can_cancel(case['now'], case['booked_at'], case['starts_at'])
                if type(result) is not bool:
                    raise TypeError()
            if len(json.dumps(result, allow_nan=False)) > 262144:
                raise ValueError()
            rows.append({'actual': result, 'error': None})
        except Exception:
            rows.append({'actual': None, 'error': '함수 실행 오류 또는 반환 형식·크기 오류'})
print(json.dumps(rows))
