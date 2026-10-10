"""队列：多进程领取无重复、锁竞争、repeat/generation 与唯一约束。"""

import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path

from shelllib.queue import TaskPool, connect


def _init_pool(path: Path, tasks=None, repeat=2, workers=4):
    pool = TaskPool(path)
    pool.initialize(tasks or ["curl/arvo_1", "curl/arvo_2"], "e2e", repeat, workers)
    return pool


class ClaimTests(unittest.TestCase):
    def test_multiprocess_claims_no_duplicate(self):
        """并发领取同一任务池：每个 (task, mode) 只被领取一次。

        结果通过每 worker 的输出文件收集（沙箱内 multiprocessing 共享
        Queue/Manager 不可用）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "queue.sqlite3"
            _init_pool(db_path, tasks=[f"p/task{i}" for i in range(20)], workers=8)

            def claim_many(rank: int, output: str):
                pool = TaskPool(db_path)
                claimed = []
                read = connect(db_path, readonly=True)
                try:
                    for _ in range(200):
                        if (read.execute(
                            "SELECT COUNT(*) FROM tasks WHERE state='done'"
                        ).fetchone()[0] == 20):
                            break
                        task = pool.claim_task(rank)
                        if task is None:
                            break
                        if task == "WAIT":
                            time.sleep(0.01)
                            continue
                        claimed.append(task["task_path"])
                        pool.finish_task_if_complete(task["id"], rank)
                finally:
                    read.close()
                Path(output).write_text("\n".join(claimed), encoding="utf-8")

            outputs = [str(tmp_path / f"claims-{rank}.txt") for rank in range(8)]
            processes = [
                multiprocessing.get_context("fork").Process(
                    target=claim_many, args=(rank, output)
                )
                for rank, output in enumerate(outputs)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=60)
                self.assertEqual(process.exitcode, 0, "worker 异常退出")

            all_claims = []
            for output in outputs:
                text = Path(output).read_text(encoding="utf-8")
                all_claims.extend(line for line in text.splitlines() if line)
            self.assertEqual(len(all_claims), len(set(all_claims)), "并发领取出现重复任务")
            self.assertEqual(
                set(all_claims),
                {f"p/task{i}" for i in range(20)},
                "并非全部任务被领取",
            )

    def test_claim_respects_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "queue.sqlite3"
            pool = _init_pool(db_path)
            pool.request_stop()
            task = pool.claim_task(0)
            self.assertIsNone(task)

    def test_lock_contention_does_not_kill_pool(self):
        """SQLite 写锁竞争：持锁者释放后写事务成功，不把整个池打死。"""
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "queue.sqlite3"
            pool = _init_pool(db_path)

            def hold_and_release():
                # 连接在本线程内创建并使用（SQLite 线程限制）
                blocker = connect(db_path)
                blocker.execute("BEGIN IMMEDIATE")
                blocker.execute("UPDATE tasks SET state='pending' WHERE id=1")
                time.sleep(1.0)
                blocker.commit()
                blocker.close()

            thread = threading.Thread(target=hold_and_release)
            thread.start()
            pool.request_stop()  # 等待持锁释放后成功写入
            thread.join(timeout=10)
            pool.clear_stop()
            task = pool.claim_task(0)
            self.assertIsNotNone(task)


class AttemptUniquenessTests(unittest.TestCase):
    def test_active_attempt_per_slot_is_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "queue.sqlite3"
            pool = _init_pool(db_path, tasks=["p/task1"], repeat=1)
            task = pool.claim_task(0)
            attempt = pool.begin_attempt(task, 1, 0, "launch-1", Path(tmp) / "a1")
            self.assertIsNotNone(attempt)
            with self.assertRaises(Exception):
                # 同一逻辑 repeat 的第二个活动 attempt 被部分唯一索引拒绝
                second = pool.begin_attempt(task, 1, 0, "launch-1", Path(tmp) / "a2")
                _ = second

    def test_generation_increments_across_abandon(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "queue.sqlite3"
            pool = _init_pool(db_path, tasks=["p/task1"], repeat=1)
            task = pool.claim_task(0)
            first = pool.begin_attempt(task, 1, 0, "launch-1", Path(tmp) / "a1")
            self.assertEqual(first["generation"], 1)
            outcome = pool.recover_dead_attempt(first, "worker died")
            self.assertEqual(outcome, "abandoned")
            second = pool.begin_attempt(task, 1, 0, "launch-1", Path(tmp) / "a2")
            self.assertEqual(second["generation"], 2)
            self.assertNotEqual(first["attempt_id"], second["attempt_id"])

    def test_finished_slot_not_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "queue.sqlite3"
            pool = _init_pool(db_path, tasks=["p/task1"], repeat=2)
            task = pool.claim_task(0)
            attempt = pool.begin_attempt(task, 1, 0, "launch-1", Path(tmp) / "a1")
            pool.finish_attempt(attempt, 0, "success", None)
            slot = pool.next_pending_slot(task["id"])
            self.assertEqual(slot["repeat_index"], 2)  # 第二个 repeat，不是重新执行第一个


class CrashWindowTests(unittest.TestCase):
    def test_recover_finished_from_receipt(self):
        """结果已落盘、DB 未提交：验证 receipt 后补记完成。"""
        from shelllib import results as results_mod

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "queue.sqlite3"
            pool = _init_pool(db_path, tasks=["p/task1"], repeat=1)
            task = pool.claim_task(0)
            attempt = pool.begin_attempt(task, 1, 0, "launch-1", tmp_path / "attempt1")
            attempt_dir = tmp_path / "attempt1"
            result = results_mod.AttemptResult(
                task_path="p/task1", mode="e2e", repeat_index=1, generation=1,
                attempt_id=attempt["attempt_id"], scope="test", launch_id="launch-1",
                model="glm53", claude_code_version="2.1.91",
                stage_records={
                    stage: results_mod.StageRecord(stage=stage, normalized_status="passed")
                    for stage in (1, 2, 3, 4)
                },
                success_stages=[1, 2, 3], enabled_stages=[1, 2, 3, 4],
            )
            results_mod.write_result_and_receipt(attempt_dir, result, [1, 2, 3, 4])

            # 模拟崩溃窗口：attempt 仍 running，但 receipt 已落盘
            recovered = pool.recover_finished_from_receipts(
                verify=lambda row: results_mod.verify_receipt(
                    Path(row["output_dir"]), [1, 2, 3, 4]
                ) is not None,
                required_stages=[1, 2, 3, 4],
            )
            self.assertEqual(recovered["recovered"], 1)
            counts = pool.counts()
            self.assertEqual(counts["finished_repeats"], 1)
            self.assertEqual(counts["outcomes"].get("success"), 1)

    def test_partial_receipt_not_recovered(self):
        """只保存部分阶段结果不等于 repeat 已完成。"""
        from shelllib import results as results_mod

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "queue.sqlite3"
            pool = _init_pool(db_path, tasks=["p/task1"], repeat=1)
            task = pool.claim_task(0)
            attempt = pool.begin_attempt(task, 1, 0, "launch-1", tmp_path / "attempt1")
            attempt_dir = tmp_path / "attempt1"
            result = results_mod.AttemptResult(
                task_path="p/task1", mode="e2e", repeat_index=1, generation=1,
                attempt_id=attempt["attempt_id"], scope="test", launch_id="launch-1",
                model="glm53", claude_code_version="2.1.91",
                stage_records={stage: results_mod.StageRecord(stage=stage, normalized_status="passed")
                               for stage in (1, 2)},  # 只有部分阶段
                success_stages=[1, 2, 3], enabled_stages=[1, 2, 3, 4],
            )
            # 手动写不完整 receipt（模拟阶段未完成时的部分落盘）
            results_mod.write_result_and_receipt(attempt_dir, result, [1, 2])
            recovered = pool.recover_finished_from_receipts(
                verify=lambda row: results_mod.verify_receipt(
                    Path(row["output_dir"]), [1, 2, 3, 4]
                ) is not None,
                required_stages=[1, 2, 3, 4],
            )
            self.assertEqual(recovered["recovered"], 0)

    def test_db_done_but_result_missing_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "queue.sqlite3"
            pool = _init_pool(db_path, tasks=["p/task1"], repeat=1)
            task = pool.claim_task(0)
            attempt = pool.begin_attempt(task, 1, 0, "launch-1", tmp_path / "missing")
            pool.finish_attempt(attempt, 0, "success", None)
            problems = pool.verify_done_results_exist()
            self.assertEqual(len(problems), 1)
            self.assertIn("result_missing", problems[0])


class FinishTaskTests(unittest.TestCase):
    def test_task_done_after_all_repeats(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "queue.sqlite3"
            pool = _init_pool(db_path, tasks=["p/task1"], repeat=2)
            task = pool.claim_task(0)
            for repeat in (1, 2):
                attempt = pool.begin_attempt(task, repeat, 0, "l1", Path(tmp) / f"a{repeat}")
                pool.finish_attempt(attempt, 0, "failed", None)
            self.assertTrue(pool.finish_task_if_complete(task["id"], 0))
            counts = pool.counts()
            self.assertEqual(counts["tasks"].get("done"), 1)
            self.assertTrue(pool.is_complete())


if __name__ == "__main__":
    unittest.main()
