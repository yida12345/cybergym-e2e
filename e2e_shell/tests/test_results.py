"""结果：阶段独立执行、缺失输入、disabled、错误分类与成功分组。"""

import tempfile
import unittest
from pathlib import Path

from shelllib import results as R
from shelllib import util as _util


def _record(stage, status, reason=None, raw=None):
    return R.StageRecord(stage=stage, raw_status=raw or status, normalized_status=status, reason=reason)


class NormalizeTests(unittest.TestCase):
    def test_official_error_becomes_validator_error(self):
        normalized, reason = R.normalize_official_status("error")
        self.assertEqual(normalized, "failed")
        self.assertEqual(reason, "validator_error")

    def test_passed_failed_unchanged(self):
        self.assertEqual(R.normalize_official_status("passed")[0], "passed")
        self.assertEqual(R.normalize_official_status("failed")[0], "failed")

    def test_none_is_error_not_failed(self):
        normalized, reason = R.normalize_official_status(None)
        self.assertEqual(normalized, "error")
        self.assertIn("judge_returned_none", reason)

    def test_unexpected_skipped_is_error(self):
        normalized, _ = R.normalize_official_status("skipped")
        self.assertEqual(normalized, "error")


class ConfiguredSuccessTests(unittest.TestCase):
    def test_all_passed_is_success(self):
        records = {1: _record(1, "passed"), 2: _record(2, "passed"), 3: _record(3, "passed")}
        evaluation = R.evaluate_configured_success(records, [1, 2, 3])
        self.assertEqual(evaluation["configured_success"], "success")

    def test_any_failed_is_failure(self):
        records = {1: _record(1, "passed"), 2: _record(2, "failed"), 3: _record(3, "passed")}
        evaluation = R.evaluate_configured_success(records, [1, 2, 3])
        self.assertEqual(evaluation["configured_success"], "failed")

    def test_missing_input_is_failure_not_indeterminate(self):
        records = {1: _record(1, "missing_input"), 2: _record(2, "passed"), 3: _record(3, "passed")}
        evaluation = R.evaluate_configured_success(records, [1, 2, 3])
        self.assertEqual(evaluation["configured_success"], "failed")
        self.assertEqual(evaluation["stage_statuses"][1], "missing_input")

    def test_infra_error_is_indeterminate(self):
        """成功条件阶段的基础设施错误：结果不可确定，单列。"""
        records = {
            1: _record(1, "passed"), 2: _record(2, "error", "infra:docker_down"),
            3: _record(3, "passed"),
        }
        evaluation = R.evaluate_configured_success(records, [1, 2, 3])
        self.assertEqual(evaluation["configured_success"], "indeterminate")
        self.assertEqual(evaluation["indeterminate_stages"], [2])

    def test_interrupted_is_indeterminate(self):
        records = {1: _record(1, "interrupted", "stop_requested")}
        evaluation = R.evaluate_configured_success(records, [1])
        self.assertEqual(evaluation["configured_success"], "indeterminate")

    def test_disabled_stage_in_success_makes_not_evaluable(self):
        """成功条件引用未启用阶段：不可评估，不伪造 false。"""
        records = {3: _record(3, "passed"), 4: _record(4, "passed")}
        evaluation = R.evaluate_configured_success(records, [1, 2, 3, 4])
        self.assertEqual(evaluation["configured_success"], "not_evaluable")
        self.assertEqual(sorted(evaluation["non_evaluable_stages"]), [1, 2])

    def test_non_success_stage_error_does_not_block(self):
        """非成功条件阶段的错误不变成额外成功门槛。"""
        records = {1: _record(1, "passed"), 2: _record(2, "passed"), 3: _record(3, "passed"),
                   4: _record(4, "error", "infra:docker")}
        evaluation = R.evaluate_configured_success(records, [1, 2, 3])
        self.assertEqual(evaluation["configured_success"], "success")
        health = R.validation_health(records, [1, 2, 3, 4])
        self.assertIn("stage4", health)


class OfficialMetricsTests(unittest.TestCase):
    def test_e2e_metrics(self):
        records = {1: _record(1, "passed"), 2: _record(2, "passed"), 3: _record(3, "passed"),
                   4: _record(4, "passed")}
        metrics = R.official_metrics(records, "e2e")
        self.assertTrue(metrics["agent_success"])
        self.assertTrue(metrics["gt_success"])
        self.assertTrue(metrics["full_success"])

    def test_disabled_stage_yields_none_not_false(self):
        records = {3: _record(3, "passed"), 4: _record(4, "passed")}
        metrics = R.official_metrics(records, "e2e")
        self.assertIsNone(metrics["agent_success"])  # 未评估，不伪造 false

    def test_patch_only_metrics(self):
        records = {3: _record(3, "passed"), 4: _record(4, "failed")}
        metrics = R.official_metrics(records, "patch-only")
        self.assertFalse(metrics["agent_success"])
        self.assertFalse(metrics["gt_success"])


class StageCoverageTests(unittest.TestCase):
    def test_coverage_denominator(self):
        records = {1: _record(1, "passed"), 2: _record(2, "failed")}
        coverage = R.stage_coverage(records, [1, 2, 3, 4])
        self.assertEqual(coverage["coverage"], 0.5)
        self.assertEqual(coverage["executed"], [1, 2])


class ReceiptTests(unittest.TestCase):
    def _result(self, attempt_id="aid-1", stages=(1, 2, 3, 4)):
        return R.AttemptResult(
            task_path="p/t", mode="e2e", repeat_index=1, generation=1,
            attempt_id=attempt_id, scope="s", launch_id="l", model="m",
            claude_code_version="2.1.91",
            stage_records={stage: _record(stage, "passed") for stage in stages},
            success_stages=[1, 2, 3], enabled_stages=[1, 2, 3, 4],
        )

    def test_write_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            attempt_dir = Path(tmp)
            receipt = R.write_result_and_receipt(attempt_dir, self._result(), [1, 2, 3, 4])
            self.assertTrue((attempt_dir / "result.json").exists())
            self.assertTrue((attempt_dir / "receipt.json").exists())
            verified = R.verify_receipt(attempt_dir, [1, 2, 3, 4])
            self.assertIsNotNone(verified)
            self.assertEqual(verified["attempt_id"], "aid-1")

    def test_verify_fails_on_tampered_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            attempt_dir = Path(tmp)
            R.write_result_and_receipt(attempt_dir, self._result(), [1, 2, 3, 4])
            (attempt_dir / "result.json").write_text("{}")
            self.assertIsNone(R.verify_receipt(attempt_dir, [1, 2, 3, 4]))

    def test_verify_fails_on_missing_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            attempt_dir = Path(tmp)
            R.write_result_and_receipt(attempt_dir, self._result(stages=(1, 2)), [1, 2])
            self.assertIsNone(R.verify_receipt(attempt_dir, [1, 2, 3, 4]))

    def test_result_json_single_check_policy(self):
        """主 checks 只表达当前成功策略；indeterminate 无分数。"""
        with tempfile.TemporaryDirectory() as tmp:
            attempt_dir = Path(tmp)
            # success
            R.write_result_and_receipt(attempt_dir, self._result("ok"), [1, 2, 3, 4])
            from shelllib import util as _util
            payload = _util.read_json(attempt_dir / "result.json")
            self.assertEqual(len(payload["checks"]), 1)
            self.assertEqual(payload["checks"][0]["score"], 1.0)
            self.assertEqual(payload["result"], "success")
            # indeterminate
            result = self._result("ind")
            result.stage_records[2] = _record(2, "error", "infra:x")
            R.write_result_and_receipt(attempt_dir, result, [1, 2, 3, 4])
            from shelllib import util as _util
            payload = _util.read_json(attempt_dir / "result.json")
            self.assertEqual(payload["checks"], [])
            self.assertEqual(payload["result"], "indeterminate")

    def test_classify_outcome(self):
        from shelllib import util

        with tempfile.TemporaryDirectory() as tmp:
            attempt_dir = Path(tmp)
            R.write_result_and_receipt(attempt_dir, self._result("s1"), [1, 2, 3, 4])
            payload = util.read_json(attempt_dir / "result.json")
            self.assertEqual(R.classify_attempt_outcome(payload)[0], "success")
            result = self._result("f1")
            result.stage_records[3] = _record(3, "failed")
            R.write_result_and_receipt(attempt_dir, result, [1, 2, 3, 4])
            payload = util.read_json(attempt_dir / "result.json")
            self.assertEqual(R.classify_attempt_outcome(payload)[0], "failed")
            result = self._result("i1")
            result.stage_records[1] = _record(1, "interrupted", "stop")
            R.write_result_and_receipt(attempt_dir, result, [1, 2, 3, 4])
            payload = util.read_json(attempt_dir / "result.json")
            outcome, error = R.classify_attempt_outcome(payload)
            self.assertEqual(outcome, "indeterminate")
            self.assertIn("stages", error)


class DisabledRecordsTests(unittest.TestCase):
    def test_disabled_never_zero(self):
        record = R.make_disabled_record(2)
        self.assertEqual(record.normalized_status, "disabled")
        self.assertIsNone(record.raw_status)


if __name__ == "__main__":
    unittest.main()
