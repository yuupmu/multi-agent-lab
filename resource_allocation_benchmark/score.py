"""Trusted parent-side scorer; expected outputs never go to candidate code."""
import copy
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('allocation_validation', ROOT/'evaluator/validate_dataset.py')
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)


def load_dataset():
    dataset = json.loads((ROOT/'evaluator/cases.json').read_text())
    key = json.loads((ROOT/'evaluator/answer_key.json').read_text())
    policy = json.loads((ROOT/'evaluator/policy.json').read_text())
    if key['cases_sha256'] != validation.digest(dataset) or key['policy_sha256'] != validation.digest(policy):
        raise ValueError('과제·정책·정답 버전이 일치하지 않습니다.')
    ids = [c['id'] for c in dataset['cases']]
    answer_ids = [a['id'] for a in key['answers']]
    if len(ids) != 150 or len(set(ids)) != 150 or ids != answer_ids:
        raise ValueError('150개 시나리오와 정답 ID가 일치해야 합니다.')
    if any(a['input_sha256'] != validation.digest(c['input']) for c,a in zip(dataset['cases'],key['answers'])):
        raise ValueError('시나리오 입력과 정답이 일치하지 않습니다.')
    return dataset, key


def grade_rows(rows):
    dataset, key = load_dataset()
    if len(rows) != len(dataset['cases']):
        raise ValueError('실행 결과 개수가 일치하지 않습니다.')
    schema = json.loads((ROOT/'agent/output_schema.json').read_text())
    results, categories = [], {}
    components = {k: {'correct': 0, 'total': 150} for k in ('event_results','final_state','notifications')}
    equal = lambda a,b: validation.canonical(a) == validation.canonical(b)
    for case, answer, row in zip(dataset['cases'], key['answers'], rows):
        actual, error = row['actual'], row['error']
        if error is None:
            try:
                validation.check_schema(actual, schema)
            except (AssertionError, TypeError, ValueError):
                error = '출력 형식 오류'
        matches = {k: error is None and equal(actual[k], answer['expected'][k]) for k in components}
        passed = all(matches.values())
        for k in components:
            components[k]['correct'] += int(matches[k])
        category = categories.setdefault(case['category'], {'correct':0, 'total':0})
        category['correct'] += int(passed); category['total'] += 1
        results.append({'id':case['id'], 'category':case['category'], 'passed':passed, 'error':error, 'components':matches})
    score = sum(r['passed'] for r in results)
    return {'benchmark_version':dataset['version'], 'benchmark':dataset['benchmark'],
            'policy_sha256':key['policy_sha256'], 'score':score, 'total':150,
            'accuracy_percent':round(score / 150 * 100, 2), 'errors':sum(r['error'] is not None for r in results),
            'categories':categories, 'components':components, 'results':results}


def grade(function):
    """Trusted reference/test functions only; the web backend uses isolation."""
    dataset, _ = load_dataset()
    rows = []
    for case in dataset['cases']:
        try:
            item = copy.deepcopy(case['input'])
            rows.append({'actual': function(item['initial_state'],item['events']), 'error':None})
        except Exception:
            rows.append({'actual':None, 'error':'함수 실행 오류'})
    return grade_rows(rows)
