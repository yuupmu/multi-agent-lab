import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend.adapters import COMMON_MONITOR_PROMPT, MockMonitor
from backend.engine import Conflict, Engine

ROOT = Path(__file__).resolve().parents[1]


def config():
    return {"project": {"name": "예약 실험"},
            "mainAgent": {"model": "mock-main", "prompt": "  직접 쓴 메인 지침\n수정 금지  "},
            "monitors": [{"id": i, "model": "mock-monitor", "concept": "관점 %s" % i,
                          "instructions": "  지침 %s\n%s  " % (i, COMMON_MONITOR_PROMPT)} for i in (1, 2, 3)]}


# Human policy fixtures, never agent inputs until explicitly answered.
ANSWERS = {"cutoff": {"hours": 24, "inclusive": True},
           "calendar": {"adjust": True, "time": "18:00", "holidays": ["2026-11-10", "2026-12-24"],
                        "timezone_offset_hours": 9, "closed_weekdays": [4, 5, 6]},
           "exceptions": {"minutes": 10, "inclusive": True, "started_blocks": True}}


def wait_for(engine, run_id, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    with engine.cv:
        while True:
            state = engine.get(run_id)
            if predicate(state):
                return state
            if state["status"] == "failed":
                raise AssertionError(state["events"][-1])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("Timed out: " + json.dumps(state, ensure_ascii=False))
            engine.cv.wait(min(remaining, 0.1))


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Engine(ROOT, data_dir=self.temp.name)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def ready(self):
        run = self.engine.start(config())
        return wait_for(self.engine, run["id"], lambda s: len(s["requests"]) == 3 and
                        all(m["status"] == "observed" for m in s["monitors"]))

    def finish(self, answers=None):
        state = self.ready()
        for request in state["requests"]:
            self.engine.answer(state["id"], request["id"], (answers or ANSWERS)[request["key"]], "추가 맥락\n원문")
        return wait_for(self.engine, state["id"], lambda s: s["status"] == "completed")

    def test_exact_prompts_shared_context_and_deduplication(self):
        state, expected = self.ready(), config()
        for call in state["calls"]:
            i = call["input"]
            prompt = expected["mainAgent"]["prompt"] if call["agent"] == "main" else expected["monitors"][int(call["agent"][-1])-1]["instructions"]
            self.assertEqual(i["prompt"], prompt)
            self.assertNotIn("2026-11-10", json.dumps(i["project_state"]))
            self.assertNotIn("answer_key", json.dumps(i))
            self.assertNotIn('closed_weekdays', i['project_state'])
            self.assertNotIn('day.weekday()', i['project_state'].get('code') or '')
        contexts = [c["input"]["project_state"] for c in state["calls"] if c["agent"].startswith("monitor")]
        self.assertEqual(len(contexts), 3)
        self.assertEqual(contexts[0], contexts[1])
        self.assertEqual(contexts[1], contexts[2])
        cutoff = next(r for r in state["requests"] if r["key"] == "cutoff")
        self.assertEqual(set(cutoff["sources"]), {1, 2})
        self.assertEqual({t["id"]: t["status"] for t in state["tasks"]},
                         {"input": "completed", "policy": "blocked", "docs": "completed"})

    def test_slow_observer_does_not_delay_requests_or_independent_work(self):
        gate = threading.Event()
        class SlowMonitor(MockMonitor):
            def invoke(self, envelope):
                if not gate.wait(5):
                    raise RuntimeError("Gate not released")
                return super().invoke(envelope)
        self.engine.monitors[3] = SlowMonitor(3)
        try:
            state = self.engine.start(config())
            state = wait_for(self.engine, state["id"], lambda s: len(s["requests"]) == 2)
            self.assertEqual(state["monitors"][2]["status"], "observing")
            self.assertEqual(state["tasks"][2]["status"], "completed")
            request = next(r for r in state["requests"] if r["key"] == "cutoff")
            self.engine.answer(state["id"], request["id"], ANSWERS["cutoff"])
            state = wait_for(self.engine, state["id"], lambda s: "cutoff" in s["policies"])
            self.assertEqual(state["policies"]["cutoff"], ANSWERS["cutoff"])
        finally:
            gate.set()

    def test_answers_verified_and_benchmark_requires_explicit_action(self):
        state = self.finish()
        self.assertEqual(state["test"]["status"], "not_run")
        self.assertTrue(all(r["status"] == "applied" for r in state["requests"]))
        self.assertEqual(len(state["decisions"]), 3)
        self.assertEqual(state["verification"]["type"], "policy_readback_and_syntax")
        self.assertIn("2026-11-10", state["artifact"]["code"])
        self.assertTrue(all(d["note"] == "추가 맥락\n원문" for d in state["decisions"]))
        self.engine.test(state["id"])
        result = wait_for(self.engine, state["id"], lambda s: s["test"]["status"] != "running")
        self.assertEqual(result["test"]["status"], "completed", result["test"])
        self.assertEqual(result["test"]["report"]["score"], 100)
        self.assertEqual(result["test"]["report"]["errors"], 0)

    def test_incorrect_human_policy_changes_score(self):
        answers = copy.deepcopy(ANSWERS)
        answers["cutoff"]["hours"] = 1
        state = self.finish(answers)
        self.engine.test(state["id"])
        result = wait_for(self.engine, state["id"], lambda s: s["test"]["status"] != "running")
        self.assertEqual(result["test"]["status"], "completed", result["test"])
        self.assertLess(result["test"]["report"]["score"], 100)

    def test_invalid_duplicate_answers_and_premature_grading(self):
        state = self.ready()
        request = next(r for r in state["requests"] if r["key"] == "cutoff")
        with self.assertRaises(Conflict):
            self.engine.test(state["id"])
        with self.assertRaises(ValueError):
            self.engine.answer(state["id"], request["id"], {"hours": True, "inclusive": True})
        self.engine.answer(state["id"], request["id"], ANSWERS["cutoff"])
        with self.assertRaises(Conflict):
            self.engine.answer(state["id"], request["id"], ANSWERS["cutoff"])
        self.engine.stop(state["id"])
        with self.assertRaises(Conflict):
            self.engine.answer(state["id"], state["requests"][-1]["id"], ANSWERS["exceptions"])

    def test_persistence_and_restarting_active_run(self):
        state = self.ready()
        self.engine.close()
        self.engine = Engine(ROOT, data_dir=self.temp.name)
        restored = self.engine.latest()
        self.assertEqual(restored["id"], state["id"])
        self.assertEqual(restored["status"], "interrupted")
        self.assertEqual(restored["config"], state["config"])
        self.assertEqual(len(restored["requests"]), 3)

    def test_modified_artifact_is_not_graded(self):
        state = self.finish()
        path = Path(self.temp.name) / state["id"] / state["artifact"]["filename"]
        path.write_text("raise RuntimeError('must not execute')")
        self.engine.test(state["id"])
        result = wait_for(self.engine, state["id"], lambda s: s["test"]["status"] != "running")
        self.assertEqual(result["test"]["status"], "failed")
        self.assertIn("변경", result["test"]["error"])


if __name__ == "__main__":
    unittest.main()
