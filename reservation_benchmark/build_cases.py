"""명시한 정답으로 100문항을 만들고 정답 구현과 대조합니다."""

import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evaluator.reference import explain

ROOT = Path(__file__).resolve().parent
TZ = timezone(timedelta(hours=9))
CASES = []
ANSWERS = {}


def dt(day, hour=12):
    return datetime.fromisoformat(f"{day}T{hour:02}:00:00").replace(tzinfo=TZ)


def add(category, current, booked, starts, expected, purpose):
    case_id = f"C{len(CASES) + 1:03}"
    case = {
        "id": case_id,
        "category": category,
        "now": current.isoformat(),
        "booked_at": booked.isoformat(),
        "starts_at": starts.isoformat(),
    }
    actual = explain(case["now"], case["booked_at"], case["starts_at"])
    if actual["allowed"] is not expected:
        raise AssertionError(f"Manual expected label differs: {case_id} {case} {actual}")
    CASES.append(case)
    ANSWERS[case_id] = {"allowed": expected, "purpose": purpose}


def ordinary_pairs(category, start_day, deadline_day, shifted):
    for index, hour in enumerate([10, 12, 14, 16, 18]):
        starts = dt(start_day, hour)
        deadline = dt(deadline_day, 18 if shifted else hour)
        # Some pairs use seconds, others minutes/hours to avoid only testing equality.
        before = [0, 1, 60, 3600, 7200][index]
        after = [1, 60, 3600, 7200, 10800][index]
        booked = deadline - timedelta(days=7)
        add(category, deadline - timedelta(seconds=before), booked, starts, True,
            "휴무 조정 후 마감 이전 또는 정확히 일치" if shifted else "24시간보다 충분히 이전")
        add(category, deadline + timedelta(seconds=after), booked, starts, False,
            "마감 초과, 예약 직후 유예 없음")


def main():
    CASES.clear()
    ANSWERS.clear()
    ordinary_pairs("평일 마감", "2026-11-03", "2026-11-02", False)
    for hour in [10, 12, 14, 16, 18]:
        starts = dt("2026-11-04", hour)
        deadline = dt("2026-11-03", hour)
        booked = dt("2026-11-01")
        add("마감 경계", deadline, booked, starts, True, "정확한 마감 시각 포함")
        add("마감 경계", deadline + timedelta(seconds=1), booked, starts, False, "마감 1초 초과")
    ordinary_pairs("금요일 휴무", "2026-11-07", "2026-11-05", True)
    ordinary_pairs("토요일 휴무", "2026-11-08", "2026-11-05", True)
    ordinary_pairs("일요일 휴무", "2026-11-09", "2026-11-05", True)
    ordinary_pairs("추가 휴일", "2026-11-11", "2026-11-09", True)

    for day, hour in [("2026-12-25", 10), ("2026-12-25", 16),
                      ("2026-12-26", 12), ("2026-12-27", 12), ("2026-12-28", 12)]:
        deadline = dt("2026-12-23", 18)
        booked = dt("2026-12-20")
        starts = dt(day, hour)
        add("연속 휴무", deadline, booked, starts, True, "가상 휴일과 금토일을 모두 건너뜀")
        add("연속 휴무", deadline + timedelta(hours=1), booked, starts, False,
            "연속 휴무로 조정된 마감 이후")

    for hour in [13, 14, 15, 16, 17]:
        current = dt("2026-11-03")
        starts = dt("2026-11-03", hour)
        add("예약 직후 유예", current, current - timedelta(minutes=10), starts, True,
            "기본 마감 초과지만 정확히 예약 후 10분")
        add("예약 직후 유예", current, current - timedelta(minutes=10, seconds=1), starts, False,
            "예약 후 10분 1초는 유예 없음")

    for index, hour in enumerate([10, 12, 14, 16, 18]):
        starts = dt("2026-11-04", hour)
        booked = starts - timedelta(minutes=5)
        add("시작과 유예 우선순위", starts - timedelta(seconds=1), booked, starts, True,
            "시작 직전에는 예약 직후 유예 적용")
        add("시작과 유예 우선순위", starts + timedelta(seconds=index), booked, starts, False,
            "정확히 시작 또는 이미 시작했으면 유예보다 우선하여 거부")

    for hour in [10, 12, 14, 16, 18]:
        current = dt("2026-11-02", hour)
        starts = dt("2026-11-05", hour)
        add("잘못된 시간 순서", current, current + timedelta(seconds=1), starts, False,
            "예약 생성 시각이 미래: 마감 이전이어도 거부")
        add("잘못된 시간 순서", starts, starts, starts, False,
            "예약 생성 시각이 이용 시작 이상")

    if len(CASES) != 100 or any(v != 10 for v in Counter(c["category"] for c in CASES).values()):
        raise AssertionError("Expected 10 categories of 10 cases")
    triples = {(c["now"], c["booked_at"], c["starts_at"]) for c in CASES}
    if len(triples) != 100:
        raise AssertionError("Duplicate input triples")
    metadata = {
        "benchmark_version": "1.0",
        "policy_sha256": hashlib.sha256((ROOT / "evaluator/policy.json").read_bytes()).hexdigest(),
    }
    for name, data in [
        ("cases.json", {**metadata, "cases": CASES}),
        ("answer_key.json", {**metadata, "answers": ANSWERS}),
    ]:
        (ROOT / "evaluator" / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(f"100문항 생성 완료: 허용 {sum(a['allowed'] for a in ANSWERS.values())}, 거부 {sum(not a['allowed'] for a in ANSWERS.values())}")


if __name__ == "__main__":
    main()
