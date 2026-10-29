import unittest

from evaluator.reference import can_cancel, explain
from score import grade, load_submission, ROOT


class BenchmarkTests(unittest.TestCase):
    def test_reference_matches_all_authored_labels(self):
        report = grade(load_submission(ROOT / "evaluator/reference.py"))
        self.assertEqual((report["score"], report["errors"]), (100, 0))
        self.assertEqual(len(report["categories"]), 10)

    def test_trivial_baselines_do_not_pass(self):
        self.assertEqual(grade(lambda *args: False)["score"], 55)
        self.assertEqual(grade(lambda *args: True)["score"], 45)

    def test_grader_rejects_non_boolean(self):
        report = grade(lambda *args: 1)
        self.assertEqual((report["score"], report["errors"]), (0, 100))

    def test_unimplemented_submission_is_not_success(self):
        report = grade(load_submission(ROOT / "agent/solution.py"))
        self.assertEqual((report["score"], report["errors"]), (0, 100))

    def test_same_instant_different_offset(self):
        local = ("2026-11-05T18:00:00+09:00", "2026-11-01T12:00:00+09:00", "2026-11-09T12:00:00+09:00")
        utc = ("2026-11-05T09:00:00+00:00", "2026-11-01T03:00:00+00:00", "2026-11-09T03:00:00+00:00")
        self.assertEqual(explain(*local), explain(*utc))
        self.assertTrue(can_cancel(*utc))

    def test_missing_timezone_and_bad_datetime(self):
        for current in ["2026-11-05T18:00:00", "not-a-date"]:
            self.assertFalse(can_cancel(current, "2026-11-01T12:00:00+09:00", "2026-11-09T12:00:00+09:00"))

    def test_holiday_chain_has_explicit_deadline(self):
        result = explain("2026-12-23T18:00:00+09:00", "2026-12-20T12:00:00+09:00", "2026-12-28T12:00:00+09:00")
        self.assertEqual(result["deadline"], "2026-12-23T18:00:00+09:00")
        self.assertTrue(result["allowed"])

    def test_started_overrides_booking_grace(self):
        result = explain("2026-11-04T12:00:00+09:00", "2026-11-04T11:55:00+09:00", "2026-11-04T12:00:00+09:00")
        self.assertEqual(result, {"allowed": False, "reason": "already_started"})


if __name__ == "__main__":
    unittest.main()
