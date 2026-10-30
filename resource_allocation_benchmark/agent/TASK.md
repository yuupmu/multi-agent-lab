# 예약·대기열·자원 배정 프로그램

다음 순수 Python 함수를 구현한다.

```python
def simulate(initial_state: dict, events: list[dict]) -> dict:
    ...
```

동일한 이용 시간대에 대한 예약 운영을 처리한다. 자원은 seats, laptops, projectors이며 initial_state에는 capacities 객체가 있다. 초기 예약은 없다. 외부 파일·네트워크·시스템 시각·난수를 사용하지 않는다.

입력 사건 종류와 필드:

| op | 필드(event_id, op 포함) |
|---|---|
| reserve | reservation_id, user_id, needs, priority |
| cancel | reservation_id |
| amend | reservation_id, needs, priority |
| set_capacity | resource, capacity |

needs는 자원 이름과 요구 수량의 객체이다. reserve는 예약 접수, cancel은 취소, amend는 필요 자원·우선순위 변경, set_capacity는 자원 총량 변경을 요청한다. 잘못된 입력과 재전송 사건도 입력에 포함될 수 있다.

반환 객체의 필드는 event_results, final_state, notifications이다. 정확한 형식은 OUTPUT_FORMAT.md와 output_schema.json을 따른다. 150세트 일괄 결과 제출에는 submission_template.json의 각 output을 해당 함수 반환 객체로 채운다.

우선순위, 선착순, 자원 부족 시 대기·거절, 건너뛰기, 중복 처리, 변경·취소·재전송·알림 등의 운영 정책은 이 문서에서 확정하지 않는다. 필요한 정책을 인간에게 질문하고, 받은 답변을 근거로 구현·검증한다. 정답 파일이나 평가용 입력을 조회하지 않는다.
