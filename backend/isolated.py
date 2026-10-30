"""macOS sandbox: model code cannot read the project/keys/answers or use networking."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def run_cases(code, cases, task_id='cancellation'):
    if task_id not in ('cancellation', 'allocation'):
        raise ValueError('알 수 없는 과제입니다.')
    if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
        raise RuntimeError('실제 모델 코드 실행에는 macOS 격리 실행기가 필요합니다.')
    if len(code) > 100000 or len(cases) > (150 if task_id == 'allocation' else 100):
        raise ValueError('검증 크기를 초과했습니다.')
    with tempfile.TemporaryDirectory(prefix='agent-check-') as temporary:
        root = Path(temporary).resolve()
        candidate = root / 'solution.py'
        candidate.write_text(code)
        runner = root / 'runner.py'
        runner.write_bytes(Path(__file__).with_name('isolated_runner.py').read_bytes())
        python = str(Path(sys.executable).resolve())
        framework_binary = Path(sys.base_prefix) / 'Resources/Python.app/Contents/MacOS/Python'
        if framework_binary.is_file():
            python = str(framework_binary.resolve())
        # Only interpreter libraries and this disposable job are readable.
        readable = ['/System', '/usr', '/Library/Developer/CommandLineTools', '/Library/Apple',
                    str(Path(sys.base_prefix).resolve()), str(root), '/dev/null', '/dev/urandom']
        profile = '(version 1) (deny default) (allow sysctl-read)\n'
        profile += '(allow process-exec (literal %s))\n' % json.dumps(python)
        profile += '(allow file-read* (literal "/") (literal "/private") (literal "/Library") (literal "/private/var"))\n'
        profile += '(allow file-read* %s)\n' % ' '.join('(subpath %s)' % json.dumps(p) for p in readable)
        try:
            result = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, python, '-I', '-B', str(runner), str(candidate)],
                                    input=json.dumps({'task_id':task_id, 'cases':cases}), capture_output=True, text=True, timeout=12,
                                    cwd=root, env={'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8'})
        except subprocess.TimeoutExpired:
            raise RuntimeError('코드 검증 제한 시간을 초과했습니다.') from None
        if result.returncode != 0:
            raise RuntimeError('격리된 코드 실행이 실패했습니다. 문법·허용 라이브러리·실행 시간을 확인하세요.')
        try:
            rows = json.loads(result.stdout)
            if not isinstance(rows, list) or len(rows) != len(cases):
                raise ValueError()
            for row in rows:
                expected_type = dict if task_id == 'allocation' else bool
                if not isinstance(row, dict) or set(row) != {'actual', 'error'} or (row['actual'] is not None and type(row['actual']) is not expected_type):
                    raise ValueError()
            return rows
        except (ValueError, TypeError):
            raise RuntimeError('격리된 코드의 반환 형식이 올바르지 않습니다.') from None


def check_code(code, checks, task_id='cancellation'):
    if not isinstance(checks, list) or len(checks) > 10:
        raise ValueError('자체 검증은 최대 10개입니다.')
    if task_id == 'allocation':
        return check_allocation(code, checks)
    for check in checks:
        if not isinstance(check, dict) or set(check) != {'now', 'booked_at', 'starts_at', 'expected'} or type(check['expected']) is not bool:
            raise ValueError('자체 검증 형식이 잘못됐습니다.')
        if any(not isinstance(check[k], str) or len(check[k]) > 80 for k in ('now', 'booked_at', 'starts_at')):
            raise ValueError('자체 검증 시각 형식이 잘못됐습니다.')
    if not checks:
        return {'passed': True, 'correct': 0, 'total': 0, 'results': []}
    rows = run_cases(code, [{k: c[k] for k in ('now', 'booked_at', 'starts_at')} for c in checks])
    results = [{**c, **r, 'passed': r['error'] is None and r['actual'] == c['expected']} for c, r in zip(checks, rows)]
    return {'passed': all(r['passed'] for r in results), 'correct': sum(r['passed'] for r in results),
            'total': len(results), 'results': results}


def grade_isolated(code, benchmark, task_id='cancellation'):
    if task_id == 'allocation':
        spec = importlib.util.spec_from_file_location('allocation_grader', str(benchmark / 'score.py'))
        grader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(grader)
        dataset, _ = grader.load_dataset()
        rows = run_cases(code, [c['input'] for c in dataset['cases']], task_id)
        return grader.grade_rows(rows)
    dataset = json.loads((benchmark / 'evaluator/cases.json').read_text())
    cases = [{k: c[k] for k in ('now', 'booked_at', 'starts_at')} for c in dataset['cases']]
    rows = iter(run_cases(code, cases))
    # The trusted parent owns the grading labels. The candidate sees inputs only.
    spec = importlib.util.spec_from_file_location('trusted_grader', str(benchmark / 'score.py'))
    grader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grader)
    def submission(now, booked_at, starts_at):
        row = next(rows)
        if row['error']:
            raise RuntimeError('격리된 함수 실행 오류')
        return row['actual']
    return grader.grade(submission)


def check_allocation(code, checks):
    from resource_allocation_benchmark.evaluator.validate_dataset import check_schema
    schema = json.loads((Path(__file__).resolve().parents[1] / 'resource_allocation_benchmark/agent/output_schema.json').read_text())
    inputs, expected = [], []
    for check in checks:
        if not isinstance(check, dict) or set(check) != {'input_json', 'expected_json'}:
            raise ValueError('자체 검증에 input_json, expected_json이 필요합니다.')
        if any(not isinstance(v, str) or len(v) > 100000 for v in check.values()):
            raise ValueError('자체 검증 JSON 크기·형식 오류')
        item, answer = json.loads(check['input_json']), json.loads(check['expected_json'])
        if (not isinstance(item, dict) or set(item) != {'initial_state', 'events'}
                or not isinstance(item['initial_state'], dict) or not isinstance(item['events'], list)
                or len(item['events']) > 100 or not isinstance(answer, dict)):
            raise ValueError('자체 검증 입력·기대 결과 형식 오류')
        try:
            check_schema(answer, schema)
        except AssertionError:
            raise ValueError('자체 검증 기대 결과가 과제 출력 규격과 다릅니다.') from None
        inputs.append(item); expected.append(answer)
    if not checks:
        return {'passed':True, 'correct':0, 'total':0, 'results':[]}
    rows = run_cases(code, inputs, 'allocation')
    canonical = lambda x: json.dumps(x, sort_keys=True, separators=(',', ':'), allow_nan=False)
    results = [{'input':item, 'expected':answer, **row,
                'passed':row['error'] is None and canonical(row['actual']) == canonical(answer)}
               for item, answer, row in zip(inputs, expected, rows)]
    return {'passed':all(r['passed'] for r in results), 'correct':sum(r['passed'] for r in results),
            'total':len(results), 'results':results}
