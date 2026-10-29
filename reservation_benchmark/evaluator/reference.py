"""평가자용 정답 구현. 에이전트 작업 공간에는 제공하지 않습니다."""

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

POLICY = json.loads(Path(__file__).with_name("policy.json").read_text())
TZ = timezone(timedelta(hours=POLICY["timezone_offset_hours"]))
HOLIDAYS = {date.fromisoformat(day) for day in POLICY["holidays"]}


def parse(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("Timezone offset required")
    return result.astimezone(TZ)


def is_closed(day: date) -> bool:
    return day.weekday() in POLICY["closed_weekdays"] or day in HOLIDAYS


def cutoff_for(starts_at: datetime) -> datetime:
    deadline = starts_at - timedelta(hours=POLICY["cutoff_hours"])
    if is_closed(deadline.date()):
        if POLICY["closed_cutoff_rule"] != "previous_open_day_at_time":
            raise ValueError("Unsupported closed-day policy")
        day = deadline.date() - timedelta(days=1)
        for _ in range(3660):
            if not is_closed(day):
                return datetime.combine(
                    day, time.fromisoformat(POLICY["previous_open_day_cutoff_time"]), TZ
                )
            day -= timedelta(days=1)
        raise ValueError("No open day found")
    return deadline


def explain(now: str, booked_at: str, starts_at: str) -> dict:
    try:
        current, booked, starts = map(parse, (now, booked_at, starts_at))
    except (ValueError, TypeError, OverflowError):
        return {"allowed": False, "reason": "invalid_datetime"}
    if booked > current or booked >= starts:
        return {"allowed": False, "reason": "invalid_chronology"}
    if current >= starts:
        return {"allowed": False, "reason": "already_started"}
    age = current - booked
    grace = timedelta(minutes=POLICY["grace_minutes"])
    within_grace = age <= grace if POLICY["grace_inclusive"] else age < grace
    if within_grace:
        return {"allowed": True, "reason": "booking_grace"}
    deadline = cutoff_for(starts)
    allowed = current <= deadline if POLICY["cutoff_inclusive"] else current < deadline
    return {
        "allowed": allowed,
        "reason": "before_deadline" if allowed else "after_deadline",
        "deadline": deadline.isoformat(),
    }


def can_cancel(now: str, booked_at: str, starts_at: str) -> bool:
    return explain(now, booked_at, starts_at)["allowed"]
