"""重新分组：只用已保存阶段证据，不改原始评分；覆盖率与不可评估。"""

import tempfile
import unittest
from pathlib import Path

from shelllib import reclassify as reclassify_mod
from shelllib import util
from shelllib.config import StagePolicy


def _harness(tmp: Path, stage_policy=None):
    from tests.helpers import make_config, make_fake_dataset

    make_fake_dataset(tmp)
    from shelllib import manifest as manifest_mod

    cfg = make_config(tmp, stage_policy=stage_policy or StagePolicy(
        mode="e2e", enabled_stages=[1, 2, 3, 4], success_stages=[1, 2, 3]))
    cfg.derive()
    manifest_mod.prepare_new_experiment(cfg)
    from shelllib.queue import TaskPool

    pool = TaskPool(cfg.queue_db_path())
    pool.initialize(["curl/arvo_66012"], "e2e", 1, 1)
    return cfg, pool


def _add_attempt(cfg, pool, *, attempt_id, stages: dict, outcome="failed", repeat=1):
    from shelllib.queue import connect

    output_dir = cfg.attempt_root / f"000001-curl_arvo_66012/r{repeat:03d}"
    output_dir.mkdir(parents=True, exist_ok=True)
    with _closing(connect(cfg.queue_db_path())) as db:
        cursor = db.execute(
            "INSERT INTO attempts(attempt_id,task_id,repeat_index,generation,state,"
            "phase,launch_id,worker_rank,started,output_dir,outcome) "
            "VALUES(?,1,?,1,'finished','done','l1',0,1.0,?,?)",
            (attempt_id, repeat, str(output_dir), outcome),
        )
        row_id = cursor.lastrowid
        for stage, status in stages.items():
            db.execute(
                "INSERT INTO stage_results(attempt_id,stage,normalized_status) VALUES(?,?,?)",
                (attempt_id, stage, status),
            )
        db.commit()
    return row_id


class _closing:
    def __init__(self, obj):
        self._obj = obj

    def __enter__(self):
        return self._obj

    def __exit__(self, *_exc):
        self._obj.close()
        return False


class ReclassifyTests(unittest.TestCase):
    def test_regroup_from_saved_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg, pool = _harness(tmp_path)
            # 阶段证据全部 passed；原始 outcome 记 failed（旧策略口径）。
            # 重新分组只看阶段证据：S1-S4 全通过 → 新策略下 success。
            _add_attempt(cfg, pool, attempt_id="a1", outcome="failed",
                         stages={1: "passed", 2: "passed", 3: "passed", 4: "passed"})
            report = reclassify_mod.reclassify(cfg, [1, 2, 3, 4])
            self.assertEqual(report["counts"]["success"], 1)
            # 原始结果未被改写
            from shelllib.queue import connect

            with _closing(connect(cfg.queue_db_path(), readonly=True)) as db:
                row = db.execute("SELECT outcome FROM attempts WHERE attempt_id='a1'").fetchone()
                self.assertEqual(row["outcome"], "failed")

    def test_disabled_stage_not_evaluable(self):
        """引用未执行阶段：不可评估 + 原因说明，不伪造失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg, pool = _harness(tmp_path, stage_policy=StagePolicy(
                mode="e2e", enabled_stages=[3, 4], success_stages=[3]))
            _add_attempt(cfg, pool, attempt_id="a1", stages={3: "passed", 4: "passed"})
            report = reclassify_mod.reclassify(cfg, [1, 2, 3])
            self.assertEqual(report["counts"]["not_evaluable"], 1)
            self.assertEqual(report["not_evaluable_count"], 1)
            self.assertIn("disabled", str(report["stage_coverage"]))
            # 输出目录独立且原始评分不动
            policy_dir = Path(report["policy_dir"])
            self.assertTrue((policy_dir / "reclassification.json").exists())
            payload = util.read_json(policy_dir / "reclassification.json")
            self.assertEqual(payload["records"][0]["configured_success"], "not_evaluable")
            self.assertIsNotNone(payload["records"][0]["reason"])

    def test_infra_error_keeps_indeterminate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg, pool = _harness(tmp_path)
            _add_attempt(cfg, pool, attempt_id="a1",
                         stages={1: "passed", 2: "error", 3: "passed", 4: "passed"})
            report = reclassify_mod.reclassify(cfg, [1, 2, 3])
            self.assertEqual(report["counts"]["indeterminate"], 1)
            self.assertEqual(report["counts"]["success"], 0)

    def test_policy_dir_isolated(self):
        """不同策略输出到不同 policy 目录，互不覆盖。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg, pool = _harness(tmp_path)
            _add_attempt(cfg, pool, attempt_id="a1",
                         stages={1: "passed", 2: "passed", 3: "passed", 4: "failed"})
            report_a = reclassify_mod.reclassify(cfg, [1, 2, 3])
            report_b = reclassify_mod.reclassify(cfg, [3, 4])
            self.assertNotEqual(report_a["policy_dir"], report_b["policy_dir"])
            self.assertEqual(report_a["counts"]["success"], 1)
            self.assertEqual(report_b["counts"]["failed"], 1)


if __name__ == "__main__":
    unittest.main()
