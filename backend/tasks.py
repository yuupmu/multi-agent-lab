"""Public task metadata. No operational answer policies."""
TASKS = {
    'cancellation': {'name': '예약 취소', 'directory': 'reservation_benchmark', 'function': 'can_cancel',
                     'arguments': ['now', 'booked_at', 'starts_at'], 'total': 100},
    'allocation': {'name': '예약·대기열·자원 배정', 'directory': 'resource_allocation_benchmark', 'function': 'simulate',
                   'arguments': ['initial_state', 'events'], 'total': 150},
}


def task_id(state):
    return state.get('config', {}).get('task_id', 'cancellation')
