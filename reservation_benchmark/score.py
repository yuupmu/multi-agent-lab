"""신뢰하는 로컬 Python 제출을 고정 100문항으로 채점합니다."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_submission(path):
    spec = importlib.util.spec_from_file_location("benchmark_submission", str(path))
    if spec is None or spec.loader is None:
        raise ValueError("Python 제출 파일을 읽을 수 없습니다.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = getattr(module, "can_cancel", None)
    if not callable(function):
        raise ValueError("can_cancel(now, booked_at, starts_at) 함수가 필요합니다.")
    return function


def grade(function):
    dataset = json.loads((ROOT / "evaluator/cases.json").read_text())
    key = json.loads((ROOT / "evaluator/answer_key.json").read_text())
    policy_hash = hashlib.sha256((ROOT / "evaluator/policy.json").read_bytes()).hexdigest()
    if not dataset["policy_sha256"] == key["policy_sha256"] == policy_hash:
        raise ValueError("정책과 테스트 버전이 다릅니다. 사례와 정답을 함께 갱신하세요.")
    cases, answers = dataset["cases"], key["answers"]
    ids = [case["id"] for case in cases]
    if len(ids) != 100 or len(set(ids)) != 100 or set(ids) != set(answers):
        raise ValueError("중복/누락 없이 100문항과 정답이 일치해야 합니다.")
    rows, categories = [], {}
    label_scores = {"allow": {"correct": 0, "total": 0}, "deny": {"correct": 0, "total": 0}}
    for case in cases:
        expected = answers[case["id"]]["allowed"]
        result, error = None, None
        try:
            result = function(case["now"], case["booked_at"], case["starts_at"])
            if type(result) is not bool:
                raise TypeError("반환값은 bool이어야 합니다.")
        except Exception as exc:
            result = None
            error = f"{type(exc).__name__}: {exc}"
        passed = error is None and result is expected
        count = categories.setdefault(case["category"], {"correct": 0, "total": 0})
        count["correct"] += int(passed)
        count["total"] += 1
        label = label_scores["allow" if expected else "deny"]
        label["correct"] += int(passed)
        label["total"] += 1
        rows.append({"id": case["id"], "category": case["category"], "expected": expected,
                     "actual": result, "passed": passed, "error": error})
    score = sum(row["passed"] for row in rows)
    return {"benchmark_version": dataset["benchmark_version"], "policy_sha256": policy_hash,
            "score": score, "total": len(rows), "accuracy_percent": score,
            "errors": sum(row["error"] is not None for row in rows),
            "categories": categories, "labels": label_scores, "results": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("submission", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = grade(load_submission(args.submission.resolve()))
    print(f"총점: {result['score']}/{result['total']} | 실행 오류: {result['errors']}개")
    for category, count in result["categories"].items():
        print(f"  {category}: {count['correct']}/{count['total']}")
    for label, count in result["labels"].items():
        print(f"  정답 {label}: {count['correct']}/{count['total']}")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(f"상세 결과: {args.output}")


if __name__ == "__main__":
    main()
