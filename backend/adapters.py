"""Replace these mock adapters when connecting a real model provider.

AgentInput always carries the verbatim user prompt separately from project state.
Mocks use fixed rules; they do NOT interpret natural-language prompts.
"""
import copy
import json
from datetime import date, time

COMMON_MONITOR_PROMPT = "전달받은 프로젝트 상태를 담당 관점으로 검토하고, ‘계속 진행 / 추가 검증 / 인간 판단 필요’ 중 하나를 제안한다. 인간 판단이 필요하면 근거·질문·영향받는 작업을 제시한다."

QUESTIONS = {
    "cutoff": {
        "question": "취소 마감 시간과 경계 포함 여부를 정해주세요.",
        "reason": "요구사항에 마감 기준이 없습니다.",
        "fields": [
            {"name": "hours", "label": "예약 시작 몇 시간 전", "type": "number", "min": 0, "max": 8760},
            {"name": "inclusive", "label": "정확한 마감 시각 포함", "type": "boolean"},
        ],
    },
    "calendar": {
        "question": "기준 시간대, 휴무 요일, 마감 조정과 추가 휴일을 정해주세요.",
        "reason": "달력과 시간대 정책이 미확정입니다.",
        "fields": [
            {"name": "timezone_offset_hours", "label": "기준 시간대 UTC 오프셋 (시간)", "type": "number", "min": -23, "max": 23},
            {"name": "closed_weekdays", "label": "휴무 요일 (월=0 ~ 일=6, 쉼표 구분 / 없으면 비움)", "type": "weekdays"},
            {"name": "adjust", "label": "휴무일 마감을 직전 영업일로 이동", "type": "boolean"},
            {"name": "time", "label": "이동한 영업일의 마감 시각", "type": "time"},
            {"name": "holidays", "label": "추가 휴일 (YYYY-MM-DD, 쉼표 구분 / 없으면 비움)", "type": "dates"},
        ],
    },
    "exceptions": {
        "question": "예약 직후 유예와 이미 시작한 예약의 우선순위를 정해주세요.",
        "reason": "예외 조건과 우선순위가 미확정입니다.",
        "fields": [
            {"name": "minutes", "label": "예약 직후 유예 (분)", "type": "number", "min": 0, "max": 10080},
            {"name": "inclusive", "label": "정확한 유예 마감 시각 포함", "type": "boolean"},
            {"name": "started_blocks", "label": "이미 시작하면 유예보다 우선하여 취소 금지", "type": "boolean"},
        ],
    },
}


def validate_answer(key, answer):
    if key not in QUESTIONS or not isinstance(answer, dict):
        raise ValueError("답변 형식이 올바르지 않습니다.")
    fields = QUESTIONS[key]["fields"]
    if set(answer) != {f["name"] for f in fields}:
        raise ValueError("모든 정책 항목을 입력하세요.")
    result = {}
    for field in fields:
        value = answer[field["name"]]
        kind = field["type"]
        if kind == "number":
            if type(value) is not int or not field["min"] <= value <= field["max"]:
                raise ValueError(field["label"] + ": 범위 내 정수가 필요합니다.")
        elif kind == "boolean":
            if type(value) is not bool:
                raise ValueError(field["label"] + ": 선택이 필요합니다.")
        elif kind == "time":
            if not isinstance(value, str) or len(value) not in (5, 8):
                raise ValueError("마감 시각을 입력하세요.")
            parsed = time.fromisoformat(value)
            if parsed.tzinfo is not None:
                raise ValueError("마감 시각은 한국 현지 시각으로 입력하세요.")
            value = parsed.isoformat()
        elif kind == 'weekdays':
            if not isinstance(value, list) or len(value) > 7 or any(type(day) is not int or not 0 <= day <= 6 for day in value):
                raise ValueError('휴무 요일은 0~6의 숫자 목록이어야 합니다.')
            value = sorted(set(value))
            if len(value) == 7:
                raise ValueError('모의 실행에는 최소 하루의 영업일이 필요합니다.')
        else:
            if not isinstance(value, list) or len(value) > 365:
                raise ValueError("휴일 목록이 올바르지 않습니다.")
            if any(not isinstance(day, str) for day in value):
                raise ValueError("휴일은 날짜 문자열이어야 합니다.")
            value = sorted({date.fromisoformat(day).isoformat() for day in value})
        result[field["name"]] = value
    return result


def agent_input(role, config, state, action):
    # Keep the exact text for reproducibility; never append private benchmark answers.
    prompt = config["prompt"] if role == "main" else config["instructions"]
    return {"role": role, "model": config["model"], "prompt": prompt,
            "concept": config.get("concept"), "action": action,
            "project_state": copy.deepcopy(state)}


class MockMainAgent:
    def invoke(self, envelope):
        if envelope["action"] == "plan":
            return {"plan": ["입력 처리", "취소 정책 구현", "사용 방법 작성"],
                    "assumptions": ["cutoff", "calendar", "exceptions"]}
        policy = envelope["project_state"]["policies"]
        if set(policy) != {'cutoff', 'calendar', 'exceptions'}:
            return {'code': 'import json\nPOLICIES = json.loads(%r)\n\ndef can_cancel(now, booked_at, starts_at):\n    raise RuntimeError("Human policy decisions are incomplete")\n' % json.dumps(policy, ensure_ascii=False)}
        template = __import__("pathlib").Path(__file__).with_name("solution_template.txt").read_text()
        return {"code": template.replace("__POLICY_JSON_LITERAL__", repr(json.dumps(policy, ensure_ascii=False)))}


class MockMonitor:
    def __init__(self, monitor_id):
        self.monitor_id = monitor_id

    def invoke(self, envelope):
        # IDs provide deterministic test coverage, not an interpretation of the persona.
        topics = {1: ["cutoff"], 2: ["cutoff", "calendar"], 3: ["exceptions"]}[self.monitor_id]
        missing = [key for key in topics if key not in envelope["project_state"]["policies"]]
        return {"decision": "human" if missing else "continue",
                "requests": [{"key": key, "affected_tasks": ["policy"], "priority": "normal",
                              **copy.deepcopy(QUESTIONS[key])} for key in missing]}
