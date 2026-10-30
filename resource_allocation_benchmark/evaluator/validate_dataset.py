"""Validate authored data and independently reconstruct state from answer events.

This is a data integrity checker, not a candidate-code runner or task solution.
"""
import copy
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESOURCES = ('seats', 'laptops', 'projectors')
FAILURES = {'invalid_event', 'duplicate_reservation', 'duplicate_user', 'unknown_reservation',
            'not_active', 'insufficient_capacity', 'capacity_below_usage', 'event_id_conflict'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def reconstruct(case, expected):
    """Separate ledger: apply authored event outcomes, then audit allocation order."""
    capacities = copy.deepcopy(case['input']['initial_state']['capacities'])
    ledger, notices, seen = {}, [], {}
    events, results = case['input']['events'], expected['event_results']
    assert len(events) == len(results)

    def available():
        return {resource: total - sum(row['needs'].get(resource, 0) for row in ledger.values()
                                     if row['status'] == 'confirmed') for resource, total in capacities.items()}

    def fits(row):
        free = available()
        return all(amount <= free[key] for key, amount in row['needs'].items())

    for index, (event, result) in enumerate(zip(events, results), 1):
        assert set(result) == {'event_id', 'code', 'replayed', 'admitted'}
        assert result['event_id'] == event['event_id'] and type(result['replayed']) is bool
        eid, code, op = event['event_id'], result['code'], event.get('op')
        encoded = canonical(event)
        if eid in seen:
            original, first_code = seen[eid]
            same = encoded == original
            assert result['replayed'] == same and result['admitted'] == []
            assert code == (first_code if same else 'event_id_conflict')
            continue
        assert not result['replayed'] and code != 'event_id_conflict'
        seen[eid] = (encoded, code)
        if code in FAILURES:
            assert not result['admitted']
            continue
        rid = event.get('reservation_id')
        if op == 'reserve':
            assert code in ('confirmed', 'queued') and rid not in ledger
            ledger[rid] = {'reservation_id': rid, 'user_id': event['user_id'], 'needs': copy.deepcopy(event['needs']),
                           'priority': event['priority'], 'status': 'waiting', 'order': index}
        elif op == 'cancel':
            assert code == 'cancelled' and ledger[rid]['status'] != 'cancelled'
            ledger[rid]['status'] = 'cancelled'
            notices.append({'event_id': eid, 'type': 'cancelled', 'reservation_id': rid})
        elif op == 'amend':
            assert code == 'amended' and ledger[rid]['status'] != 'cancelled'
            ledger[rid]['needs'] = copy.deepcopy(event['needs'])
            ledger[rid]['priority'] = event['priority']
        elif op == 'set_capacity':
            assert code == 'capacity_updated'
            capacities[event['resource']] = event['capacity']
        else:
            raise AssertionError('success on unknown operation')
        ranked = sorted((r for r in ledger.values() if r['status'] == 'waiting'), key=lambda r: (-r['priority'], r['order']))
        expected_admitted = iter(result['admitted'])
        for row in ranked:
            if fits(row):
                assert next(expected_admitted, None) == row['reservation_id'], (case['id'], index, 'allocation order')
                row['status'] = 'confirmed'
                notices.append({'event_id': eid, 'type': 'confirmed', 'reservation_id': row['reservation_id']})
        assert next(expected_admitted, None) is None
        if op == 'reserve':
            assert code == ('confirmed' if ledger[rid]['status'] == 'confirmed' else 'queued')
        assert all(value >= 0 for value in available().values())
        active_users = [r['user_id'] for r in ledger.values() if r['status'] != 'cancelled']
        assert len(active_users) == len(set(active_users))
    actual = {'capacities': capacities, 'available': available(), 'reservations': [ledger[rid] for rid in sorted(ledger)],
              'waitlist': [r['reservation_id'] for r in sorted(ledger.values(), key=lambda r: (-r['priority'], r['order'])) if r['status'] == 'waiting']}
    assert canonical(actual) == canonical(expected['final_state']), (case['id'], 'final state')
    assert canonical(notices) == canonical(expected['notifications']), (case['id'], 'notifications')


def check_schema(value, schema, path='$'):
    """Only the standard JSON Schema keywords used by this dataset's schema."""
    type_name = schema.get('type')
    types = {'object': dict, 'array': list, 'string': str, 'integer': int, 'boolean': bool}
    if type_name:
        assert type(value) is types[type_name], (path, type_name)
    if 'enum' in schema:
        assert value in schema['enum'], (path, 'enum')
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        assert set(schema.get('required', [])) <= set(value), (path, 'required')
        if schema.get('additionalProperties') is False:
            assert set(value) <= set(properties), (path, 'extra field')
        for key, item in value.items():
            if key in properties:
                check_schema(item, properties[key], path+'.'+key)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            check_schema(item, schema.get('items', {}), path+'[%d]' % i)
    elif type(value) is int:
        assert value >= schema.get('minimum', value), (path, 'minimum')
        assert value <= schema.get('maximum', value), (path, 'maximum')
    elif isinstance(value, str):
        assert len(value) >= schema.get('minLength', 0), (path, 'minLength')


def main():
    dataset = json.loads((ROOT/'cases.json').read_text())
    key = json.loads((ROOT/'answer_key.json').read_text())
    policy = json.loads((ROOT/'policy.json').read_text())
    template = json.loads((ROOT.parent/'agent/submission_template.json').read_text())
    schema = json.loads((ROOT.parent/'agent/output_schema.json').read_text())
    coverage = json.loads((ROOT/'coverage.json').read_text())
    assert key['policy_sha256'] == digest(policy)
    assert key['cases_sha256'] == digest(dataset)
    cases, answers = dataset['cases'], key['answers']
    assert len(cases) == len(answers) == len(template['answers']) == 150
    assert len({case['id'] for case in cases}) == 150
    assert len({digest(case['input']) for case in cases}) == 150
    assert Counter(case['category'] for case in cases) == {c['id']: c['count'] for c in coverage['categories']}
    count = 0
    for case, answer, blank in zip(cases, answers, template['answers']):
        assert case['id'] == answer['id'] == blank['id'] and blank['output'] is None
        assert answer['input_sha256'] == digest(case['input'])
        check_schema(answer['expected'], schema)
        reconstruct(case, answer['expected'])
        count += len(case['input']['events'])
    assert count == coverage['total_events']
    print('PASS: 150 scenarios, %s events, 15 categories, schemas, checksums, allocation order, final states and notifications.' % count)


if __name__ == '__main__':
    main()
