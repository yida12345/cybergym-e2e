"""resume：stop/resume 循环、stale supervisor、worker 恢复与结果提交崩溃窗口。

run_attempt 端到端使用脚本化 Docker（无真实容器/模型）：验证阶段独立
执行（S1 失败仍执行 S2/S3/S4）、停止中断、receipt 恢复与历史保留。
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from shelllib import manifest as manifest_mod
from shelllib import results as results_mod
from shelllib import util
from shelllib.config import StagePolicy
from shelllib.docker_control import DockerControl
from shelllib.image_manager import ImageManager
from shelllib.queue import TaskPool
from shelllib.runner_adapter import AttemptContext, run_attempt
from tests.helpers import (
    FakeDockerRunner,
    make_config,
    make_fake_dataset,
    make_fake_popen_factory,
)

ACR_IMAGE = "reg.example.com/e2e/img@sha256:bbb"


class _Harness:
    """一次测试的完整环境：冻结输入 + 队列 + 脚本化 Docker。"""

    def __init__(self, tmp: Path, **cfg_overrides):
        make_fake_dataset(tmp)
        self.cfg = make_config(tmp, **cfg_overrides)
        self.cfg.derive()
        self.manifest = manifest_mod.prepare_new_experiment(self.cfg)
        self.pool = TaskPool(self.cfg.queue_db_path())
        tasks = [task["task_path"] for task in self.manifest["tasks"]]
        self.pool.initialize(tasks, self.cfg.stage_policy.mode, self.cfg.task_repeat_n, 1)
        self.runner = FakeDockerRunner()
        self.runner.pull_image(ACR_IMAGE)
        self.docker = DockerControl(
            runner=self.runner,
            api_timeout=10,
            timeout_retries=1,
            retry_delay_seconds=0,
            startup_lock_dir=self.cfg.docker_startup_lock_dir,
            startup_concurrency=4,
            popen=make_fake_popen_factory(self.runner),
        )
        self.images = ImageManager(
            self.docker,
            self.cfg.output_root / "shared_state",
            self.cfg.run_scope / "state" / "pulled_images.jsonl",
            lock_root=self.cfg.state_dir / "image-locks",
        )

    def attempt_context(self, task_path="curl/arvo_66012", repeat=1, stop=lambda: None):
        task_row = self._task_row(task_path)
        attempt_id = f"t{task_row['id']:06d}-e2e-r{repeat:03d}-g001-test0001"
        attempt_dir = self.cfg.attempt_root / f"{task_row['id']:06d}-curl_arvo_66012" / f"r{repeat:03d}"
        return AttemptContext(
            cfg=self.cfg, manifest=self.manifest, docker=self.docker,
            images=self.images, launch_id="launch-test", scope=self.cfg.scope,
            task_path=task_path, mode=self.cfg.stage_policy.mode,
            repeat_index=repeat, generation=1, attempt_id=attempt_id,
            attempt_dir=attempt_dir,
            network_info={
                "internal_network": "cg-e2e-internal-x",
                "install_network": "cg-e2e-install-x",
                "squid_url": "http://10.0.0.2:3128",
                "capture_base_url": "http://10.0.0.1:30305/glm53",
            },
            stop_check=lambda: stop() or None,
            api_key="sk-test",
        )

    def _task_row(self, task_path):
        from shelllib.queue import connect

        with _closing(connect(self.cfg.queue_db_path(), readonly=True)) as db:
            return db.execute(
                "SELECT * FROM tasks WHERE task_path=?", (task_path,)
            ).fetchone()


class _closing:
    def __init__(self, obj):
        self._obj = obj

    def __enter__(self):
        return self._obj

    def __exit__(self, *_exc):
        self._obj.close()
        return False


def _script_stage(runner: FakeDockerRunner, stage: int, status: str, container_suffix: str):
    """为某个验证容器脚本化 validation_entry 输出。"""
    result = {
        "schema_version": 1, "only_stage": stage,
        "results": {
            f"stage{stage}": {"status": status, "output": f"scripted {status}", "description": "d"}
        },
    }
    runner.script_exec(
        f"cg-e2e-s{stage}-", "validation_entry",
        (0, json.dumps(result), ""),
    )


class RunAttemptTests(unittest.TestCase):
    def test_stages_run_independently_after_failure(self):
        """S1 失败仍执行启用的 S2/S3/S4；disabled 不填 0。"""
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp))
            # 容器名按阶段前缀脚本化：S1 failed，S2/S3/S4 passed
            _script_stage(h.runner, 1, "failed", "s1")
            for stage in (2, 3, 4):
                _script_stage(h.runner, stage, "passed", f"s{stage}")
            h.docker.popen = popen_with_artifacts(h.runner, poc=True, patch=True)
            outcome = run_attempt(h.attempt_context())
            payload = util.read_json(Path(outcome.attempt_dir) / "result.json")
            stages = payload["stages"]
            self.assertEqual(stages["1"]["normalized_status"], "failed")
            self.assertEqual(stages["2"]["normalized_status"], "passed")
            self.assertEqual(stages["3"]["normalized_status"], "passed")
            self.assertEqual(stages["4"]["normalized_status"], "passed")
            # S1+S2+S3 成功条件因 S1 失败而 failed
            self.assertEqual(payload["configured_success"]["configured_success"], "failed")
            self.assertEqual(outcome.outcome, "failed")

    def test_missing_poc_only_affects_s1_s2(self):
        """只启用 S1 时缺少补丁不阻止 S1；反过来缺 PoC 只影响 S1/S2。"""
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp), stage_policy=StagePolicy(
                mode="e2e", enabled_stages=[1, 2, 3, 4], success_stages=[1]))
            # Agent 只生成 patch，不生成 poc
            h.docker.popen = popen_with_artifacts(h.runner, poc=False, patch=True)
            _script_stage(h.runner, 3, "passed", "s3")
            _script_stage(h.runner, 4, "passed", "s4")
            outcome = run_attempt(h.attempt_context())
            payload = util.read_json(Path(outcome.attempt_dir) / "result.json")
            self.assertEqual(payload["stages"]["1"]["normalized_status"], "missing_input")
            self.assertEqual(payload["stages"]["2"]["normalized_status"], "missing_input")
            self.assertEqual(payload["stages"]["3"]["normalized_status"], "passed")
            self.assertEqual(payload["stages"]["4"]["normalized_status"], "passed")
            self.assertEqual(outcome.outcome, "failed")

    def test_official_error_normalized_to_validator_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp))
            h.docker.popen = popen_with_artifacts(h.runner, poc=True, patch=True)
            _script_stage(h.runner, 1, "error", "s1")
            for stage in (2, 3, 4):
                _script_stage(h.runner, stage, "passed", f"s{stage}")
            outcome = run_attempt(h.attempt_context())
            payload = util.read_json(Path(outcome.attempt_dir) / "result.json")
            stage1 = payload["stages"]["1"]
            self.assertEqual(stage1["normalized_status"], "failed")
            self.assertEqual(stage1["reason"], "validator_error")
            self.assertEqual(stage1["raw_status"], "error")

    def test_stop_during_agent_interrupts(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp))
            stop_flag = {"reason": None}

            def stop_check():
                return stop_flag["reason"]

            # CC 开始后立刻请求停止
            h.docker.popen = stopping_cc_popen(h.runner, stop_flag)
            outcome = run_attempt(h.attempt_context(stop=lambda: stop_flag["reason"]))
            payload = util.read_json(Path(outcome.attempt_dir) / "result.json")
            # 中断的 attempt：所有启用阶段 interrupted，不进入普通失败集合
            for stage in (1, 2, 3, 4):
                self.assertEqual(payload["stages"][str(stage)]["normalized_status"], "interrupted")
            self.assertEqual(payload["result"], "indeterminate")

    def test_disabled_stages_recorded_as_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp), stage_policy=StagePolicy(
                mode="patch-only", enabled_stages=[3, 4], success_stages=[3]))
            h.docker.popen = popen_with_artifacts(h.runner, poc=False, patch=True)
            _script_stage(h.runner, 3, "passed", "s3")
            _script_stage(h.runner, 4, "failed", "s4")
            outcome = run_attempt(h.attempt_context())
            payload = util.read_json(Path(outcome.attempt_dir) / "result.json")
            self.assertEqual(payload["stages"]["1"]["normalized_status"], "disabled")
            self.assertEqual(payload["stages"]["2"]["normalized_status"], "disabled")
            self.assertEqual(payload["stages"]["3"]["normalized_status"], "passed")
            # patch-only 模式给容器复制 crash.log/poc.bin
            self.assertEqual(payload["mode"], "patch-only")
            self.assertEqual(outcome.outcome, "success")

    def test_no_feedback_or_summary_model_calls(self):
        """单次执行不调用轨迹摘要模型：无 call_llm 相关 exec。"""
        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp))
            h.docker.popen = popen_with_artifacts(h.runner, poc=True, patch=True)
            for stage in (1, 2, 3, 4):
                _script_stage(h.runner, stage, "passed", f"s{stage}")
            run_attempt(h.attempt_context())
            for _container, command in h.runner.exec_log:
                self.assertNotIn("call_llm", command)
                self.assertNotIn("summarize", command)


class StopResumeCycleTests(unittest.TestCase):
    def _seed_attempt(self, pool, tmp, task_id=1, repeat=1):
        task_row = pool.claim_task(0)
        attempt = pool.begin_attempt(task_row, repeat, 0, "launch-1", tmp / f"a{repeat}")
        return task_row, attempt

    def test_stop_resume_skips_finished_reruns_interrupted(self):
        """手动停止后不改配置再次运行：已完成 repeat 跳过，中断的重跑。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pool = TaskPool(tmp_path / "q.sqlite3")
            pool.initialize(["p/t1"], "e2e", 2, 1)
            task, a1 = self._seed_attempt(pool, tmp_path)
            # 第一个 repeat 完成
            pool.finish_attempt(a1, 0, "success", None)
            # 第二个 repeat 开始后被停止（未完成）
            a2 = pool.begin_attempt(task, 2, 0, "launch-1", tmp_path / "a2")
            pool.request_stop()
            # 停止恢复：interrupted pool 归位
            pool.recover_interrupted_pool()
            pool.clear_stop()
            counts = pool.counts()
            self.assertEqual(counts["finished_repeats"], 1)
            self.assertEqual(counts["slots"]["pending"], 1)
            # 恢复后领取：新 generation 执行第二个 repeat
            task2 = pool.claim_task(0)
            self.assertEqual(task2["task_path"], "p/t1")
            a2b = pool.begin_attempt(task2, 2, 0, "launch-2", tmp_path / "a2b")
            self.assertEqual(a2b["generation"], 2)

            def _attempt_state(pool, attempt_id):
                from shelllib.queue import connect
                with _closing(connect(pool.path, readonly=True)) as db:
                    return db.execute(
                        "SELECT state FROM attempts WHERE attempt_id=?", (attempt_id,)
                    ).fetchone()[0]

            self.assertEqual(_attempt_state(pool, a2["attempt_id"]), "abandoned")

    def test_repeated_stop_resume_preserves_history(self):
        """反复 stop/resume 不覆盖历史：attempt 记录只增不改。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pool = TaskPool(tmp_path / "q.sqlite3")
            pool.initialize(["p/t1"], "e2e", 3, 1)
            task, a1 = self._seed_attempt(pool, tmp_path)
            pool.finish_attempt(a1, 0, "success", None)
            def _attempt_state(pool, attempt_id):
                from shelllib.queue import connect
                with _closing(connect(pool.path, readonly=True)) as db:
                    return db.execute(
                        "SELECT state FROM attempts WHERE attempt_id=?", (attempt_id,)
                    ).fetchone()[0]

            for _round in range(3):
                a_next = pool.begin_attempt(task, 2, 0, f"launch-{_round}", tmp_path / "x")
                pool.request_stop()
                pool.recover_interrupted_pool()
                pool.clear_stop()
                self.assertEqual(_attempt_state(pool, a_next["attempt_id"]), "abandoned")
            from shelllib.queue import connect

            with _closing(connect(tmp_path / "q.sqlite3", readonly=True)) as db:
                states = [row["state"] for row in db.execute("SELECT state FROM attempts ORDER BY id")]
            self.assertEqual(states, ["finished", "abandoned", "abandoned", "abandoned"])
            # 第一个 repeat 的结果未被覆盖
            self.assertEqual(pool.counts()["outcomes"].get("success"), 1)

    def test_worker_recovery_with_receipt(self):
        """worker 异常退出：有有效 receipt 的 attempt 补记完成，不再重跑。"""
        from shelllib.supervisor import _recover_dead_worker

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            h = _Harness(tmp_path)
            task, attempt = self._seed_attempt(h.pool, tmp_path)
            # 写入完整结果与 receipt（模拟 worker 在 DB 提交前崩溃）
            result = results_mod.AttemptResult(
                task_path="curl/arvo_66012", mode="e2e", repeat_index=1,
                generation=attempt["generation"], attempt_id=attempt["attempt_id"],
                scope=h.cfg.scope, launch_id="launch-1", model="glm53",
                claude_code_version="2.1.91",
                stage_records={s: results_mod.StageRecord(stage=s, normalized_status="passed")
                               for s in (1, 2, 3, 4)},
                success_stages=[1, 2, 3], enabled_stages=[1, 2, 3, 4],
            )
            results_mod.write_result_and_receipt(
                Path(attempt["output_dir"]), result, [1, 2, 3, 4]
            )
            _recover_dead_worker(h.pool, h.cfg, 0, 1)
            counts = h.pool.counts()
            self.assertEqual(counts["finished_repeats"], 1)
            # 再次恢复不重复（receipt 已消费）
            _recover_dead_worker(h.pool, h.cfg, 0, 1)
            self.assertEqual(h.pool.counts()["finished_repeats"], 1)

    def test_worker_recovery_without_receipt_reruns(self):
        from shelllib.supervisor import _recover_dead_worker

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            h = _Harness(tmp_path)
            self._seed_attempt(h.pool, tmp_path)
            _recover_dead_worker(h.pool, h.cfg, 0, 1)
            counts = h.pool.counts()
            self.assertEqual(counts["finished_repeats"], 0)
            # repeat_n=2：两个 slot 都未完成（被中断的回到 pending，未开始的保持 pending）
            self.assertEqual(counts["slots"]["pending"], 2)
            self.assertNotIn("running", counts["slots"])


class StaleSupervisorTests(unittest.TestCase):
    def test_stale_pid_file_detected(self):
        """supervisor 被直接 kill -9 后 state 仍 RUNNING：PID 核对识别 stale。"""
        from shelllib.supervisor import read_pid_file, supervisor_alive

        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp))
            payload = {
                "pid": 999999999,  # 不存在的 PID
                "pid_start": "12345",
                "boot_id": util.boot_id(),
                "launch_id": "l1", "scope": h.cfg.scope,
            }
            util.atomic_write_json(h.cfg.state_dir / "supervisor.pid", payload)
            alive, reason = supervisor_alive(h.cfg)
            self.assertFalse(alive)
            self.assertEqual(reason, "pid_start_mismatch")

    def test_boot_id_change_detected(self):
        from shelllib.supervisor import read_pid_file, supervisor_alive

        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp))
            payload = {
                "pid": os.getpid(), "pid_start": util.pid_start_ticks(os.getpid()),
                "boot_id": "different-boot-id-000",
                "launch_id": "l1", "scope": h.cfg.scope,
            }
            util.atomic_write_json(h.cfg.state_dir / "supervisor.pid", payload)
            alive, reason = supervisor_alive(h.cfg)
            self.assertFalse(alive)
            self.assertEqual(reason, "boot_id_changed")

    def test_live_supervisor_of_other_command_rejected(self):
        """PID 存活但命令行不是本 scope supervisor：拒绝。"""
        from shelllib.supervisor import supervisor_alive

        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp))
            payload = {
                "pid": os.getpid(),
                "pid_start": util.pid_start_ticks(os.getpid()),
                "boot_id": util.boot_id(),
                "launch_id": "l1", "scope": h.cfg.scope,
            }
            util.atomic_write_json(h.cfg.state_dir / "supervisor.pid", payload)
            alive, reason = supervisor_alive(h.cfg)
            self.assertFalse(alive)
            self.assertEqual(reason, "command_mismatch")

    def test_no_pid_file(self):
        from shelllib.supervisor import supervisor_alive

        with tempfile.TemporaryDirectory() as tmp:
            h = _Harness(Path(tmp))
            alive, reason = supervisor_alive(h.cfg)
            self.assertFalse(alive)
            self.assertEqual(reason, "no_pid_file")


# ---------------------------------------------------------------------------
# Popen 脚本：给 Agent 容器塞入不同的产物组合
# ---------------------------------------------------------------------------


def popen_with_artifacts(runner, *, poc: bool, patch: bool):
    """CC 执行时在 fake 容器内生成指定产物。"""
    base = make_fake_popen_factory(runner)

    def factory(command, stdout=None, stderr=None, start_new_session=True):
        from tests.helpers import _find_container

        container = _find_container(runner, command)
        body = " ".join(command)
        if container and "claude" in body:
            info = runner._by_reference(container)
            out = runner.tmp / info["Id"] / "output"
            out.mkdir(parents=True, exist_ok=True)
            if poc:
                (out / "poc.bin").write_bytes(b"\x01agentpoc")
            if patch:
                (out / "fix.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")
        return base(command, stdout, stderr, start_new_session)

    return factory


def stopping_cc_popen(runner, stop_flag):
    base = make_fake_popen_factory(runner)

    def factory(command, stdout=None, stderr=None, start_new_session=True):
        from tests.helpers import _find_container

        container = _find_container(runner, command)
        body = " ".join(command)
        if container and "claude" in body:
            stop_flag["reason"] = "stop_requested"  # CC 一启动就停止
        return base(command, stdout, stderr, start_new_session)

    return factory


if __name__ == "__main__":
    unittest.main()
