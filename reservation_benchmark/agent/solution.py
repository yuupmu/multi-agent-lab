"""에이전트가 구현할 제출 틀. 외부 라이브러리는 사용하지 않습니다."""


def can_cancel(now: str, booked_at: str, starts_at: str) -> bool:
    raise NotImplementedError("운영 정책을 확인한 뒤 구현하세요.")
