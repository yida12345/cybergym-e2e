"""supervisor 与 worker：任务池生命周期、异常恢复、watchdog 与自动收尾。

单 supervisor 模型：启动锁 + PID/start + boot_id + scope 共同核对，避免
PID 复用和双重调度。worker 不持续写心跳，supervisor 统一更新状态。

watchdog 覆盖：宿主磁盘低水位、attempt 产物大小、容器可写层、总
attempt 超时。终态 result 后的静默由 cc_runner 的 idle guard 处理，只按
已知终态事件启动计时，不把正常等待模型响应误判为静默异常。

结果提交顺序：run_attempt 先落盘 result+receipt，worker 再提交数据库
完成状态；“结果已落盘、DB 尚未提交”的崩溃窗口由恢复流程验证 receipt
后补记完成，避免重复调用模型。
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
import shutil
import signal
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import util
from .config import ShellConfig, total_attempt_budget_seconds
from .docker_control import DockerControl
from .image_manager import ImageManager
from .prompts import load_prompt_functions
from .queue import TaskPool, connect
from .results import verify_receipt
from .runner_adapter import AttemptContext, run_attempt

TERMINATE = False


def _signal_handler(_signum, _frame):
    global TERMINATE
    TERMINATE = True


class Watchdog:
    """attempt 级资源监控；昂贵检查按间隔限频。"""

    def __init__(
        self,
        *,
        run_scope: Path,
        attempt_dir: Path,
        scope: str,
        docker: DockerControl,
        min_free_gb: float,
        max_output_gb: float,
        max_container_rw_gb: float,
        total_budget_seconds: float,
        disk_check_interval: float,
    ):
        self.run_scope = run_scope
        self.attempt_dir = Path(attempt_dir)
        self.scope = scope
        self.docker = docker
        self.min_free_gb = min_free_gb
        self.max_output_gb = max_output_gb
        self.max_container_rw_gb = max_container_rw_gb
        self.clock = util.TimeoutClock(total_budget_seconds)
        self.disk_check_interval = disk_check_interval
        self._last_expensive = 0.0
        self._cached_reason: str | None = None

    def check(self, *, stop_requested: bool) -> str | None:
        if stop_requested:
            return "stop_requested"
        if self.clock.expired():
            return "attempt_budget_exceeded"
        now = time.monotonic()
        if now - self._last_expensive < self.disk_check_interval:
            return self._cached_reason
        self._last_expensive = now
        free_gb = shutil.disk_usage(self.run_scope).free / (1024**3)
        if free_gb < self.min_free_gb:
            self._cached_reason = f"host_disk_low free_gb={free_gb:.1f}"
            return self._cached_reason
        size_gb = util.dir_size_bytes(self.attempt_dir) / (1024**3)
        if size_gb > self.max_output_gb:
            self._cached_reason = f"attempt_output_limit size_gb={size_gb:.1f}"
            return self._cached_reason
        containers = self.docker.list_scope_containers(self.scope)
        rw_gb = self.docker.container_writable_bytes(containers) / (1024**3)
        if rw_gb > self.max_container_rw_gb:
            self._cached_reason = f"container_writable_limit size_gb={rw_gb:.1f}"
            return self._cached_reason
        self._cached_reason = None
        return None


@dataclass
class WorkerPlan:
    """worker 进程所需的序列化参数（spawn 传递）。"""

    cfg: ShellConfig
    manifest: dict[str, Any]
    launch_id: str
    network_info: dict[str, Any]
    api_key: str
    prompt_hashes: dict[str, str]
    rank: int


def worker_main(plan: WorkerPlan) -> None:
    """worker 进程主循环：领取任务，串行运行独立 repeat。"""
    global TERMINATE
    TERMINATE = False
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    cfg = plan.cfg
    pool = TaskPool(cfg.queue_db_path())
    docker = DockerControl(
        api_timeout=cfg.docker_api_timeout_seconds,
        timeout_retries=cfg.docker_timeout_retries,
        retry_delay_seconds=cfg.docker_retry_delay_seconds,
        startup_lock_dir=cfg.docker_startup_lock_dir,
        startup_concurrency=cfg.docker_startup_concurrency,
    )
    images = ImageManager(
        docker, cfg.output_root / "shared_state",
        cfg.run_scope / "state" / "pulled_images.jsonl",
        cleanup_enabled=cfg.image_cleanup_enabled,
    )
    pool.register_worker(plan.rank, os.getpid())
    read_db = connect(cfg.queue_db_path(), readonly=True)

    def stop_requested() -> bool:
        if TERMINATE:
            return True
        row = read_db.execute("SELECT value FROM meta WHERE key='stop_requested'").fetchone()
        return bool(row and row[0] == "1")

    try:
        while not stop_requested():
            task = pool.claim_task(plan.rank)
            if task is None:
                return
            if task == "WAIT":
                time.sleep(cfg.status_poll_seconds)
                continue
            _run_task_repeats(plan, pool, docker, images, task, stop_requested)
            with contextlib.suppress(Exception):
                pool.finish_task_if_complete(task["id"], plan.rank)
    finally:
        read_db.close()
        with contextlib.suppress(Exception):
            pool.update_worker(plan.rank, "finished")


def _run_task_repeats(
    plan: WorkerPlan,
    pool: TaskPool,
    docker: DockerControl,
    images: ImageManager,
    task: Any,
    stop_requested: Callable[[], bool],
) -> None:
    cfg = plan.cfg
    while not stop_requested():
        slot = pool.next_pending_slot(task["id"])
        if slot is None:
            return
        attempt = pool.begin_attempt(
            task, slot["repeat_index"], plan.rank, plan.launch_id,
            cfg.attempt_root / _attempt_dir_name(task, slot["repeat_index"]),
        )
        pool.mark_attempt_running(attempt, plan.rank, os.getpid())
        attempt_dir = Path(attempt["output_dir"])
        attempt_dir.mkdir(parents=True, exist_ok=True)

        watchdog = Watchdog(
            run_scope=cfg.run_scope,
            attempt_dir=attempt_dir,
            scope=cfg.scope,
            docker=docker,
            min_free_gb=cfg.min_host_free_gb,
            max_output_gb=cfg.max_attempt_output_gb,
            max_container_rw_gb=cfg.max_container_rw_gb,
            total_budget_seconds=total_attempt_budget_seconds(cfg),
            disk_check_interval=cfg.disk_check_interval_seconds,
        )

        def stop_check() -> str | None:
            return watchdog.check(stop_requested=stop_requested())

        context = AttemptContext(
            cfg=cfg,
            manifest=plan.manifest,
            docker=docker,
            images=images,
            launch_id=plan.launch_id,
            scope=cfg.scope,
            task_path=task["task_path"],
            mode=task["mode"],
            repeat_index=slot["repeat_index"],
            generation=attempt["generation"],
            attempt_id=attempt["attempt_id"],
            attempt_dir=attempt_dir,
            network_info=plan.network_info,
            stop_check=stop_check,
            api_key=plan.api_key,
            prompt_hashes=plan.prompt_hashes,
        )
        outcome = run_attempt(context)

        # 阶段明细先入 stage_results（结果已落盘，即使随后崩溃也有证据）
        stage_payloads = []
        if outcome.result_json:
            for stage_text, record in (outcome.result_json.get("stages") or {}).items():
                stage_payloads.append(
                    {
                        "stage": int(stage_text.replace("stage", "")),
                        "raw_status": record.get("raw_status"),
                        "normalized_status": record.get("normalized_status"),
                        "reason": record.get("reason"),
                        "duration_seconds": record.get("duration_seconds"),
                        "exit_code": (record or {}).get("exit_code"),
                        "evidence_path": (record.get("evidence_paths") or {}).get("result_json"),
                    }
                )
        with contextlib.suppress(Exception):
            pool.store_stage_results(attempt["attempt_id"], stage_payloads)

        receipt = verify_receipt(attempt_dir, cfg.stage_policy.enabled_stages)
        pool.finish_attempt(
            attempt,
            plan.rank,
            outcome.outcome,
            outcome.error,
            receipt_hash=(receipt or {}).get("result_sha256"),
            count_repeat=True,
        )


def _attempt_dir_name(task: Any, repeat_index: int) -> Path:
    import re

    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", task["task_path"]).strip("_")
    return Path(f"{int(task['id']):06d}-{safe}") / f"r{repeat_index:03d}"


# ---------------------------------------------------------------------------
# supervisor
# ---------------------------------------------------------------------------


def supervisor_main(cfg: ShellConfig, launch_id: str, network_info: dict[str, Any]) -> None:
    """单 supervisor：启动 worker 池、异常恢复、停止与自动收尾。"""
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    pool = TaskPool(cfg.queue_db_path())
    manifest = util.read_json(cfg.manifest_path())
    _, prompt_hashes = load_prompt_functions(cfg.repo_dir)
    api_key = os.environ.get(cfg.model_api_key_env, "")

    pool.begin_launch(launch_id, os.getpid(), cfg.workers, network_info.get("capture_dir", ""))
    _write_pid_file(cfg, launch_id)

    context = multiprocessing.get_context("spawn")
    children: dict[int, multiprocessing.process.BaseProcess] = {}

    def start_worker(rank: int) -> None:
        plan = WorkerPlan(
            cfg=cfg, manifest=manifest, launch_id=launch_id,
            network_info=network_info, api_key=api_key,
            prompt_hashes=prompt_hashes, rank=rank,
        )
        child = context.Process(target=worker_main, args=(plan,), name=f"cg-e2e-worker-{rank}")
        child.start()
        children[rank] = child

    for rank in range(cfg.workers):
        start_worker(rank)

    failed = False
    db = connect(cfg.queue_db_path())
    try:
        while True:
            if TERMINATE:
                pool.request_stop()
            with contextlib.suppress(sqlite3.OperationalError):
                pool.heartbeat()
            stopped = _stop_requested(db)
            unfinished = db.execute("SELECT COUNT(*) FROM tasks WHERE state!='done'").fetchone()[0]

            for rank, child in list(children.items()):
                child.join(timeout=0)
                if child.is_alive():
                    continue
                worker_row = db.execute(
                    "SELECT restarts,state FROM workers WHERE rank=?", (rank,)
                ).fetchone()
                if stopped or unfinished == 0 or worker_row["state"] == "finished":
                    children.pop(rank)
                    continue
                _recover_dead_worker(pool, cfg, rank, child.exitcode)
                restarts = db.execute(
                    "SELECT restarts FROM workers WHERE rank=?", (rank,)
                ).fetchone()[0]
                if restarts > cfg.worker_max_restarts:
                    pool.update_worker(rank, "error", "restart budget exhausted")
                    children.pop(rank)
                    failed = True
                else:
                    start_worker(rank)

            if unfinished == 0:
                with contextlib.suppress(Exception):
                    pool.update_worker_state_bulk("finished")
                break
            if stopped:
                break
            if not any(child.is_alive() for child in children.values()):
                failed = True
                break
            time.sleep(min(cfg.status_poll_seconds, 2.0))
    finally:
        if TERMINATE:
            pool.request_stop()
        for child in children.values():
            if child.is_alive():
                child.terminate()
        for child in children.values():
            child.join(timeout=cfg.stop_grace_seconds)
        remaining = db.execute("SELECT COUNT(*) FROM tasks WHERE state!='done'").fetchone()[0]
        controller_state = "complete" if remaining == 0 else "attention" if failed else "stopped"
        pool.finish_launch(controller_state)
        db.close()
        _remove_pid_file(cfg)

    if controller_state == "complete" and cfg.auto_finalize_on_complete:
        _auto_finalize(cfg, launch_id)
    if failed and controller_state != "complete":
        raise RuntimeError("task pool stopped with unfinished tasks; inspect 06_status.sh")


def _recover_dead_worker(pool: TaskPool, cfg: ShellConfig, rank: int, exitcode: int | None) -> None:
    """回收异常 worker：先验证 receipt，避免重复调用模型。

    run_attempt 在 worker 进程内执行，attempt.worker_rank 即归属；
    恢复步骤本身受 scope 启动锁保护，不会与第二个 worker 抢同一 repeat
    （活动 attempt 的 slot 保持 running 状态，直到此处显式回收）。
    """
    for attempt in pool.active_attempts_for_worker(rank):
        receipt = verify_receipt(Path(attempt["output_dir"]), cfg.stage_policy.enabled_stages)
        pool.recover_dead_attempt(
            attempt,
            f"worker rank={rank} exited unexpectedly (exit={exitcode})",
            receipt_valid=receipt is not None,
        )
    pool.recover_dead_worker(rank, exitcode)


def _stop_requested(db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT value FROM meta WHERE key='stop_requested'").fetchone()
    return bool(row and row[0] == "1")


def _write_pid_file(cfg: ShellConfig, launch_id: str) -> None:
    payload = {
        "pid": os.getpid(),
        "pid_start": util.pid_start_ticks(os.getpid()),
        "boot_id": util.boot_id(),
        "launch_id": launch_id,
        "scope": cfg.scope,
        "written_at": util.utc_now_iso(),
    }
    util.atomic_write_json(cfg.state_dir / "supervisor.pid", payload)


def _remove_pid_file(cfg: ShellConfig) -> None:
    with contextlib.suppress(OSError):
        (cfg.state_dir / "supervisor.pid").unlink()


def read_pid_file(cfg: ShellConfig) -> dict[str, Any] | None:
    path = cfg.state_dir / "supervisor.pid"
    if not path.exists():
        return None
    try:
        return util.read_json(path)
    except (OSError, ValueError):
        return None


def supervisor_alive(cfg: ShellConfig) -> tuple[bool, str]:
    """PID 文件 + boot_id + /proc 启动时刻 + 命令行共同核对。"""
    payload = read_pid_file(cfg)
    if payload is None:
        return False, "no_pid_file"
    pid = payload.get("pid")
    if not isinstance(pid, int):
        return False, "invalid_pid"
    if payload.get("boot_id") and payload.get("boot_id") != util.boot_id():
        return False, "boot_id_changed"
    expected_start = payload.get("pid_start")
    if util.pid_start_ticks(pid) != expected_start:
        return False, "pid_start_mismatch"
    command = util.pid_command(pid)
    if "orchestrator.py" not in command or "supervise" not in command:
        return False, "command_mismatch"
    return True, "alive"


def _auto_finalize(cfg: ShellConfig, launch_id: str) -> None:
    """COMPLETE 后自动收尾：drain capture，再离线后处理。"""
    from . import postprocess

    capture_dir = Path(cfg.run_scope) / "capture_logs" / f"launch_{launch_id}"
    drained = postprocess.wait_capture_drain(capture_dir, cfg.capture_drain_timeout_seconds)
    status = "drained" if drained else "drain_timeout_archived"
    util.atomic_write_json(
        Path(cfg.run_scope) / "summary" / "capture_drain.json",
        {"launch_id": launch_id, "status": status, "waited": cfg.capture_drain_timeout_seconds},
    )
    try:
        postprocess.finalize_and_summarize(cfg, allow_incomplete=False)
        _set_export_status(cfg, "done")
    except Exception as exc:  # noqa: BLE001 - 后处理失败不回滚队列
        _set_export_status(cfg, f"ATTENTION: {exc}")


def _set_export_status(cfg: ShellConfig, value: str) -> None:
    pool = TaskPool(cfg.queue_db_path())
    with contextlib.closing(connect(cfg.queue_db_path())) as db:
        from .queue import write_transaction

        write_transaction(
            db,
            lambda c: c.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('export_status',?)", (value,)
            ),
        )
