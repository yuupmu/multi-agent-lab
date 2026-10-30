"""Evaluator-only reference; never an agent input."""
import copy
import json
KINDS = ['seats', 'laptops', 'projectors']

class Oracle:
    def __init__(self, capacities):
        self.cap = dict(capacities)
        self.rows = {}
        self.cache = {}
        self.notices = []
        self.results = []

    def used(self):
        return {k: sum(r['needs'].get(k, 0) for r in self.rows.values() if r['status'] == 'confirmed') for k in KINDS}

    def queue(self):
        return sorted((r for r in self.rows.values() if r['status'] == 'waiting'), key=lambda r: (-r['priority'], r['order']))

    def fits(self, needs, old=None):
        used = self.used()
        return all(used[k] - (old or {}).get(k, 0) + needs.get(k, 0) <= self.cap[k] for k in KINDS)

    def promote(self, eid):
        admitted = []
        for row in self.queue():
            if self.fits(row['needs']):
                row['status'] = 'confirmed'
                admitted.append(row['reservation_id'])
                self.notices.append({'event_id': eid, 'type': 'confirmed', 'reservation_id': row['reservation_id']})
        return admitted

    def valid_needs(self, needs):
        return (isinstance(needs, dict) and 'seats' in needs and set(needs) <= set(KINDS)
                and all(type(v) is int and v > 0 for v in needs.values()))

    def step(self, event, index):
        event = copy.deepcopy(event)
        eid = event['event_id']
        fingerprint = json.dumps(event, sort_keys=True, separators=(',', ':'))
        if eid in self.cache:
            original, prior = self.cache[eid]
            result = {'event_id': eid, 'code': prior['code'] if original == fingerprint else 'event_id_conflict',
                      'replayed': original == fingerprint, 'admitted': []}
            self.results.append(result)
            return result
        op = event.get('op')
        rid = event.get('reservation_id')
        row = self.rows.get(rid) if isinstance(rid, str) else None
        code, admitted = 'invalid_event', []
        base = {'event_id', 'op'}
        if op == 'reserve':
            good = (set(event) == base | {'reservation_id', 'user_id', 'needs', 'priority'}
                    and isinstance(rid, str) and bool(rid)
                    and isinstance(event['user_id'], str) and bool(event['user_id'])
                    and self.valid_needs(event['needs']) and type(event['priority']) is int and 0 <= event['priority'] <= 3)
            if good:
                if rid in self.rows:
                    code = 'duplicate_reservation'
                elif any(r['user_id'] == event['user_id'] and r['status'] != 'cancelled' for r in self.rows.values()):
                    code = 'duplicate_user'
                else:
                    self.rows[rid] = {'reservation_id': rid, 'user_id': event['user_id'], 'needs': event['needs'],
                                      'priority': event['priority'], 'status': 'waiting', 'order': index}
                    admitted = self.promote(eid)
                    code = 'confirmed' if self.rows[rid]['status'] == 'confirmed' else 'queued'
        elif op in ('cancel', 'amend'):
            required = base | {'reservation_id'} | ({'needs', 'priority'} if op == 'amend' else set())
            good = set(event) == required and isinstance(rid, str) and bool(rid)
            if op == 'amend':
                good = good and self.valid_needs(event.get('needs')) and type(event.get('priority')) is int and 0 <= event['priority'] <= 3
            if good:
                if not row:
                    code = 'unknown_reservation'
                elif row['status'] == 'cancelled':
                    code = 'not_active'
                elif op == 'cancel':
                    row['status'] = 'cancelled'
                    self.notices.append({'event_id': eid, 'type': 'cancelled', 'reservation_id': rid})
                    admitted = self.promote(eid)
                    code = 'cancelled'
                elif row['status'] == 'confirmed' and not self.fits(event['needs'], row['needs']):
                    code = 'insufficient_capacity'
                else:
                    row.update(needs=event['needs'], priority=event['priority'])
                    admitted = self.promote(eid)
                    code = 'amended'
        elif op == 'set_capacity':
            resource, amount = event.get('resource'), event.get('capacity')
            if set(event) == base | {'resource', 'capacity'} and resource in KINDS and type(amount) is int and amount >= 0:
                if amount < self.used()[resource]:
                    code = 'capacity_below_usage'
                else:
                    self.cap[resource] = amount
                    admitted = self.promote(eid)
                    code = 'capacity_updated'
        result = {'event_id': eid, 'code': code, 'replayed': False, 'admitted': admitted}
        self.cache[eid] = (fingerprint, copy.deepcopy(result))
        self.results.append(result)
        self.invariants()
        return result

    def invariants(self):
        assert all(0 <= n <= self.cap[k] for k, n in self.used().items())
        active = [r['user_id'] for r in self.rows.values() if r['status'] != 'cancelled']
        assert len(active) == len(set(active))
        assert not any(self.fits(r['needs']) for r in self.queue())

    def output(self):
        return {'event_results': self.results,
                'final_state': {'capacities': self.cap, 'available': {k: self.cap[k]-n for k, n in self.used().items()},
                                'reservations': [self.rows[k] for k in sorted(self.rows)],
                                'waitlist': [r['reservation_id'] for r in self.queue()]},
                'notifications': self.notices}


def simulate(initial_state, events):
    engine = Oracle(initial_state['capacities'])
    for index, event in enumerate(events, 1):
        engine.step(event, index)
    return engine.output()
