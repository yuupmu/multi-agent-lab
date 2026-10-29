"""Trusted child process; receives cases only, never keys or benchmark answers."""
import contextlib
import importlib.util
import json
import resource
import sys

resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))

class Sink:
    def write(self, text):
        return len(text)
    def flush(self):
        pass

cases = json.loads(sys.stdin.read())
with contextlib.redirect_stdout(Sink()), contextlib.redirect_stderr(Sink()):
    spec = importlib.util.spec_from_file_location('submission', sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = []
    for case in cases:
        try:
            result = module.can_cancel(case['now'], case['booked_at'], case['starts_at'])
            if type(result) is not bool:
                raise TypeError()
            rows.append({'actual': result, 'error': None})
        except Exception:
            rows.append({'actual': None, 'error': '함수 실행 오류 또는 bool이 아닌 반환값'})
print(json.dumps(rows))
