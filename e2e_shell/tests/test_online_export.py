"""在线导出：发布中断恢复、幂等与版本目录。"""

import json
import tempfile
import unittest
from pathlib import Path

from shelllib import util
from shelllib.online_export import OnlineExporter


def _seed_finished_attempt(cfg, *, repeat=1, outcome="success", attempt_id=None):
    from shelllib.queue import TaskPool

    pool = TaskPool(cfg.queue_db_path())
    if not cfg.queue_db_path().exists():
        pool.initialize(["curl/arvo_66012"], "e2e", 1, 1)
    from shelllib.queue import connect

    attempt_id = attempt_id or f"aid-r{repeat}"
    output_dir = cfg.attempt_root / f"000001-curl_arvo_66012/r{repeat:03d}"
    output_dir.mkdir(parents=True, exist_ok=True)
    util.atomic_write_json(output_dir / "result.json", {
        "schema_version": 2, "result": outcome,
        "checks": [{"name": "configured_success", "score": 1.0 if outcome == "success" else 0.0}],
        "configured_success": {"configured_success": outcome},
    })
    projects = output_dir / "cc_home" / "projects" / "curl"
    projects.mkdir(parents=True, exist_ok=True)
    (projects / "s.jsonl").write_text('{"type":"assistant"}\n', encoding="utf-8")
    with _closing(connect(cfg.queue_db_path())) as db:
        db.execute(
            "INSERT INTO attempts(attempt_id,task_id,repeat_index,generation,state,"
            "phase,launch_id,worker_rank,started,output_dir,outcome) "
            "VALUES(?,1,?,1,'finished','done','l1',0,1.0,?,?)",
            (attempt_id, repeat, str(output_dir), outcome),
        )
        db.commit()
    return output_dir


class _closing:
    def __init__(self, obj):
        self._obj = obj

    def __enter__(self):
        return self._obj

    def __exit__(self, *_exc):
        self._obj.close()
        return False


class _Harness:
    def __init__(self, tmp: Path):
        from tests.helpers import make_config, make_fake_dataset

        make_fake_dataset(tmp)
        from shelllib import manifest as manifest_mod

        self.cfg = make_config(tmp)
        self.cfg.derive()
        manifest_mod.prepare_new_experiment(self.cfg)
        from shelllib.queue import TaskPool

        pool = TaskPool(self.cfg.queue_db_path())
        pool.initialize(["curl/arvo_66012"], "e2e", 1, 1)


class OnlineExportTests(unittest.TestCase):
    def test_first_publish_and_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp))
            _seed_finished_attempt(h.cfg, repeat=1)
            exporter = OnlineExporter(h.cfg)
            report1 = exporter.run_once()
            self.assertEqual(report1["new_samples"], 1)
            self.assertEqual(report1["total_successful"], 1)
            self.assertTrue((h.cfg.run_scope / "online" / "successful.jsonl").exists())
            # 幂等：无新增时不产生新样本，但发布新版本目录
            report2 = exporter.run_once()
            self.assertEqual(report2["new_samples"], 0)
            self.assertEqual(report2["total_successful"], 1)

    def test_incremental_adds_new_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp))
            exporter = OnlineExporter(h.cfg)
            _seed_finished_attempt(h.cfg, repeat=1, outcome="success")
            exporter.run_once()
            _seed_finished_attempt(h.cfg, repeat=2, outcome="failed")
            report = exporter.run_once()
            self.assertEqual(report["new_samples"], 1)
            self.assertEqual(report["total_failed"], 1)
            successful = (h.cfg.run_scope / "online" / "successful.jsonl").read_text()
            failed = (h.cfg.run_scope / "online" / "failed.jsonl").read_text()
            self.assertIn("aid-r1", successful)
            self.assertIn("aid-r2", failed)

    def test_publish_interruption_recovered(self):
        """发布中断（current 未切换，pending 残留）：下次运行恢复并重建。"""
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp))
            _seed_finished_attempt(h.cfg, repeat=1)
            exporter = OnlineExporter(h.cfg)
            with exporter._lock():
                exporter._init_state()
                # 模拟中断：版本目录开始构建但未提交
                version = exporter.commits / "v1"
                version.mkdir(parents=True)
                util.atomic_write_json(
                    exporter.state / "pending.json",
                    {"version": "v1", "started": "now"},
                )
            # 下一次运行恢复：未完成的 v1 被清理并重建
            report = exporter.run_once()
            self.assertEqual(report["new_samples"], 1)
            self.assertTrue((exporter.state / "current").is_symlink())
            self.assertFalse((exporter.state / "pending.json").exists())

    def test_second_instance_rejected_by_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp))
            _seed_finished_attempt(h.cfg, repeat=1)
            exporter = OnlineExporter(h.cfg)
            with exporter._lock():
                exporter._init_state()
                with self.assertRaises(RuntimeError):
                    exporter.run_once()

    def test_results_marked_provisional(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp))
            _seed_finished_attempt(h.cfg, repeat=1)
            exporter = OnlineExporter(h.cfg)
            exporter.run_once()
            payload = util.read_json(h.cfg.run_scope / "online" / "manifest.json")
            self.assertTrue(payload["provisional"])
            first_line = (h.cfg.run_scope / "online" / "successful.jsonl").read_text().splitlines()[0]
            record = json.loads(first_line)
            self.assertTrue(record["provisional"])


if __name__ == "__main__":
    unittest.main()
