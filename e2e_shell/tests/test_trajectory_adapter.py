"""轨迹适配：capture 聚合去重/冲突、attempt 视图与 repeat/launch 隔离。"""

import json
import tempfile
import unittest
from pathlib import Path

from shelllib import util
from shelllib.trajectory_adapter import (
    aggregate_capture_logs,
    attempt_key,
    build_attempt_view,
)


def _make_capture(launch_dir: Path, capture_id: str, message_id: str = "msg_1",
                  status: str = "complete"):
    """在 launch 目录下构造一个已收尾 capture。"""
    completed = launch_dir / "raw" / "completed" / capture_id
    completed.mkdir(parents=True, exist_ok=True)
    (completed / "request.json").write_text(json.dumps({
        "capture_id": capture_id,
        "body": {"model": "glm53", "messages": [{"role": "user", "content": "hi"}]},
    }), encoding="utf-8")
    (completed / "response.json").write_text(json.dumps({
        "capture_id": capture_id,
        "message_id": message_id,
        "message": {"id": message_id, "role": "assistant", "content": "ok"},
        "proxy_delivery": "delivered",
    }), encoding="utf-8")
    (completed / "state.json").write_text(json.dumps({"state": status}), encoding="utf-8")
    (completed / "response.body").write_text("{}", encoding="utf-8")
    (completed / "sse_events.jsonl").write_text(
        json.dumps({"event": "message_start", "data": {"message": {"id": message_id}}}) + "\n",
        encoding="utf-8",
    )
    return completed


def _make_attempt_row(db_path: Path, *, task_path="curl/arvo_66012", task_id=1,
                      repeat=1, generation=1, outcome="success", attempt_id=None):
    from shelllib.queue import TaskPool

    pool = TaskPool(db_path)
    if not db_path.exists():
        pool.initialize([task_path], "e2e", 1, 1)
    from shelllib.queue import connect

    with _closing(connect(db_path)) as db:
        db.execute(
            "INSERT INTO attempts(attempt_id,task_id,repeat_index,generation,state,"
            "phase,launch_id,worker_rank,started,output_dir,outcome) "
            "VALUES(?,?,?,?,'finished','done','l1',0,1.0,?,?)",
            (attempt_id or f"aid-r{repeat}-g{generation}", task_id, repeat,
             generation, str(db_path.parent / f"attempt-{repeat}-{generation}"), outcome),
        )
        db.commit()
    return attempt_id or f"aid-r{repeat}-g{generation}"


class _closing:
    def __init__(self, obj):
        self._obj = obj

    def __enter__(self):
        return self._obj

    def __exit__(self, *_exc):
        self._obj.close()
        return False


def _make_attempt_output(root: Path, *, result="success", checks_score=1.0, session=True):
    root.mkdir(parents=True, exist_ok=True)
    result_json = {
        "schema_version": 2, "task": "curl/arvo_66012", "mode": "e2e",
        "result": result,
        "checks": [{"name": "configured_success", "score": checks_score}],
        "configured_success": {"configured_success": result},
        "stages": {str(s): {"normalized_status": "passed"} for s in (1, 2, 3, 4)},
    }
    util.atomic_write_json(root / "result.json", result_json)
    if session:
        projects = root / "cc_home" / "projects" / "curl"
        projects.mkdir(parents=True, exist_ok=True)
        (projects / "session-abc.jsonl").write_text(
            json.dumps({"type": "assistant", "message": {"id": "msg_1"}}) + "\n",
            encoding="utf-8",
        )
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "logs" / "console.log").write_text('{"type":"system"}\n', encoding="utf-8")


class CaptureAggregationTests(unittest.TestCase):
    def test_dedup_same_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_scope = tmp_path / "run"
            for launch in ("launch_1", "launch_2"):
                _make_capture(run_scope / "capture_logs" / launch, "cap-1")
            report = aggregate_capture_logs(run_scope, tmp_path / "agg")
            self.assertEqual(report["captures"], 1)
            self.assertEqual(report["duplicates"], 1)

    def test_conflict_kept_not_overwritten(self):
        """同 ID 不同内容：保留冲突副本，不覆盖。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_scope = tmp_path / "run"
            first = _make_capture(run_scope / "capture_logs" / "launch_1", "cap-1", message_id="msg_A")
            _make_capture(run_scope / "capture_logs" / "launch_2", "cap-1", message_id="msg_B")
            report = aggregate_capture_logs(run_scope, tmp_path / "agg")
            self.assertEqual(len(report["conflicts"]), 1)
            # 聚合视图保留先见内容
            kept = util.read_json(tmp_path / "agg" / "raw" / "completed" / "cap-1" / "response.json")
            self.assertEqual(kept["message_id"], "msg_A")
            # 冲突副本存在
            conflicts = list((tmp_path / "agg" / "conflicts").iterdir())
            self.assertEqual(len(conflicts), 1)
            _ = first

    def test_inflight_and_retry_listed_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_scope = tmp_path / "run" / "capture_logs" / "launch_1"
            inflight = run_scope / "raw" / "inflight" / "cap-inflight"
            inflight.mkdir(parents=True)
            retry = run_scope / "raw" / "retry_attempts" / "grp1" / "cap-retry"
            retry.mkdir(parents=True)
            _make_capture(run_scope, "cap-done")
            report = aggregate_capture_logs(tmp_path / "run", tmp_path / "agg")
            self.assertEqual(report["captures"], 1)
            self.assertGreaterEqual(report["inflight_listed"], 2)


class AttemptViewTests(unittest.TestCase):
    def test_view_layout_and_isolation(self):
        """不同 repeat/generation/launch 的 attempt 生成独立 key。"""
        key1 = attempt_key(1, "curl/arvo_66012", 1, 1, "aid1")
        key2 = attempt_key(1, "curl/arvo_66012", 2, 1, "aid2")
        key3 = attempt_key(1, "curl/arvo_66012", 1, 2, "aid3")
        self.assertNotEqual(key1, key2)
        self.assertNotEqual(key1, key3)

    def test_build_view_groups_and_single_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "queue.sqlite3"
            from shelllib.queue import TaskPool

            pool = TaskPool(db_path)
            pool.initialize(["curl/arvo_66012"], "e2e", 1, 1)
            for repeat, outcome in ((1, "success"), (2, "failed")):
                aid = _make_attempt_row(db_path, repeat=repeat, outcome=outcome,
                                       attempt_id=f"aid-r{repeat}")
                _make_attempt_output(tmp_path / f"attempt-{repeat}-1",
                                     result=outcome,
                                     checks_score=1.0 if outcome == "success" else 0.0)
            report = build_attempt_view(db_path, tmp_path / "view",
                                        stage_success_policy=[1, 2, 3])
            self.assertEqual(report["attempts"], 2)
            self.assertEqual(report["attempts_with_sessions"], 2)
            # 成功/失败分组目录
            self.assertTrue((tmp_path / "view" / "successful").is_dir())
            self.assertTrue((tmp_path / "view" / "failed").is_dir())
            # 每个视图目录：result.json + logs/projects + logs/<key>/console.log
            for group in ("successful", "failed"):
                task_dirs = list((tmp_path / "view" / group).iterdir())
                self.assertEqual(len(task_dirs), 1)
                payload = util.read_json(task_dirs[0] / "result.json")
                self.assertEqual(len(payload["checks"]), 1)
                sessions = list((task_dirs[0] / "logs" / "projects").rglob("*.jsonl"))
                self.assertEqual(len(sessions), 1)
                consoles = list((task_dirs[0] / "logs").glob("*/console.log"))
                self.assertEqual(len(consoles), 1)

    def test_indeterminate_not_exported_to_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "queue.sqlite3"
            from shelllib.queue import TaskPool

            pool = TaskPool(db_path)
            pool.initialize(["curl/arvo_66012"], "e2e", 1, 1)
            aid = _make_attempt_row(db_path, repeat=1, outcome="indeterminate",
                                    attempt_id="aid-ind")
            _make_attempt_output(tmp_path / "attempt-1-1", result="indeterminate")
            report = build_attempt_view(db_path, tmp_path / "view",
                                        stage_success_policy=[1, 2, 3])
            # indeterminate 不进入 successful/failed
            groups = [d.name for d in (tmp_path / "view").iterdir() if d.is_dir()]
            self.assertNotIn("successful", groups)
            self.assertNotIn("failed", groups)
            self.assertEqual(report["excluded"], [{"attempt_id": "aid-ind",
                                                   "problem": "outcome_indeterminate_excluded"}])


if __name__ == "__main__":
    unittest.main()
