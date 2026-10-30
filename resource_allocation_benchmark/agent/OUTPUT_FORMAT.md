# 반환·제출 형식

`simulate(initial_state, events)`는 아래 구조의 JSON 직렬화 가능한 dict를 반환한다. 아래는 구조만 보여주는 빈 결과이며 정책 정답 예시가 아니다.

```json
{
  "event_results": [],
  "final_state": {
    "capacities": {"seats": 0, "laptops": 0, "projectors": 0},
    "available": {"seats": 0, "laptops": 0, "projectors": 0},
    "reservations": [],
    "waitlist": []
  },
  "notifications": []
}
```

| 위치 | 각 원소의 필드 |
|---|---|
| event_results | event_id: str, code: str, replayed: bool, admitted: list[str] |
| final_state.reservations | reservation_id: str, user_id: str, needs: dict[str,int], priority: int, status: str, order: int |
| final_state.waitlist | 예약 ID 문자열 |
| notifications | event_id: str, type: str, reservation_id: str |

- code 값: confirmed, queued, cancelled, amended, capacity_updated, invalid_event, duplicate_reservation, duplicate_user, unknown_reservation, not_active, insufficient_capacity, capacity_below_usage, event_id_conflict.
- status 값: confirmed, waiting, cancelled.
- notification.type 값: confirmed, cancelled.
- order는 예약이 처음 정상 접수된 입력 사건의 1부터 시작하는 위치이다.
- event_results는 입력 순서, reservations는 예약 ID 사전순이다. 대기열·admitted·알림의 순서는 운영 정책에 따른다.
- 부가 필드는 넣지 않는다. 키 순서는 무관하지만 배열 순서와 값의 타입은 구분한다.

일괄 제출은 아래 구조를 따른다. submission_template.json에는 150개 ID가 모두 있고 output은 null이다. null은 미제출 표시이며 실제 정답 형식으로 인정되지 않는다.

```json
{"benchmark": "resource-allocation-v1", "answers": [{"id": "example-case-id", "output": null}]}
```

에이전트의 실제 출력에는 정책 설명·해설·추론 과정을 섞지 않는다.
