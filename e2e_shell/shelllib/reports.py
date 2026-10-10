"""状态面板（06）与汇总口径：吞吐、ETA、分母与覆盖率。

分母纪律：planned attempts、finished attempts、valid attempts 和
exported trajectories 是不同分母，绝不互相混用。同时输出官方口径、
当前 configured_success、S4 信息、完整指定漏洞成功口径和各条件的
可评估覆盖率。
"""

from __future__ import annotations

import time
from typing import Any

from .config import ShellConfig

def summarize_outcomes(cfg: ShellConfig) -> dict[str, Any]:
    """汇总已完成 repeat 的结果分布（各自分母分别列出）。"""
    from .queue import TaskPool

    pool = TaskPool(cfg.queue_db_path())
    counts = pool.counts()
    outcomes: dict[str, int] = counts["outcomes"]
    finished = counts["finished_repeats"]
    planned = counts["planned_repeats"]
    valid = sum(
        outcomes.get(name, 0) for name in ("success", "failed")
    )
    tasks_total = counts["total_tasks"]
    tasks_done = counts["tasks"].get("done", 0)

    with_any_success = 0
    with_valid_results = 0
    from .queue import connect

    with _closing(connect(cfg.queue_db_path(), readonly=True)) as db:
        for row in db.execute(
            "SELECT t.id, SUM(CASE WHEN a.outcome IN ('success','failed') THEN 1 ELSE 0 END) v, "
            "SUM(CASE WHEN a.outcome='success' THEN 1 ELSE 0 END) s "
            "FROM tasks t LEFT JOIN attempts a ON a.task_id=t.id AND a.state='finished' "
            "GROUP BY t.id"
        ):
            if row["v"]:
                with_valid_results += 1
                if row["s"]:
                    with_any_success += 1
        first = db.execute(
            "SELECT MIN(started) FROM attempts WHERE state='finished'"
        ).fetchone()[0]
        s4_stats = {
            row["normalized_status"]: row["n"]
            for row in db.execute(
                "SELECT sr.normalized_status, COUNT(DISTINCT a.attempt_id) n "
                "FROM stage_results sr JOIN attempts a ON a.attempt_id=sr.attempt_id "
                "WHERE sr.stage=4 AND a.state='finished' GROUP BY sr.normalized_status"
            )
        }
        agent_success = db.execute(
            "SELECT COUNT(DISTINCT a.attempt_id) FROM attempts a WHERE a.state='finished' "
            "AND a.outcome='success'"
        ).fetchone()[0]

    elapsed = (time.time() - first) if first else 0.0
    throughput = (finished / elapsed) if elapsed > 0 else 0.0
    remaining = planned - finished
    eta = (remaining / throughput) if throughput > 0 and remaining > 0 else None

    return {
        "tasks_total": tasks_total,
        "tasks_done": tasks_done,
        "planned_repeats": planned,
        "finished_repeats": finished,
        "abandoned_attempts": counts["abandoned_attempts"],
        "outcomes": outcomes,
        "valid_results": valid,
        "valid_success_rate": (outcomes.get("success", 0) / valid) if valid else 0.0,
        "tasks_with_valid_results": with_valid_results,
        "tasks_any_success": with_any_success,
        "official_metrics": {
            "agent_success_attempts": agent_success,
            "s4_stage_statuses": s4_stats,
        },
        "throughput_per_second": round(throughput, 6),
        "eta_seconds": round(eta, 1) if eta is not None else None,
        "meta": counts["meta"],
    }

def status_payload(cfg: ShellConfig) -> dict[str, Any]:
    """06 状态面板数据；只读连接。"""
    from .queue import TaskPool
    from . import util as _util
    from .supervisor import read_pid_file, supervisor_alive

    pool = TaskPool(cfg.queue_db_path())
    counts = pool.counts()
    alive, alive_reason = supervisor_alive(cfg)
    pid_payload = read_pid_file(cfg)
    outcome_summary = summarize_outcomes(cfg)

    workers = []
    from .queue import connect

    with _closing(connect(cfg.queue_db_path(), readonly=True)) as db:
        for row in db.execute(
            """SELECT w.*, t.task_path, a.attempt_id, a.phase AS attempt_phase
               FROM workers w
               LEFT JOIN tasks t ON t.id = w.current_task
               LEFT JOIN attempts a ON a.attempt_id = (
                 SELECT a2.attempt_id FROM attempts a2
                 WHERE a2.task_id = w.current_task AND a2.repeat_index = w.current_repeat
                   AND a2.state IN ('starting','running')
                 ORDER BY a2.id DESC LIMIT 1)
               ORDER BY w.rank"""
        ):
            item = dict(row)
            item["alive"] = bool(
                item.get("pid")
                and item.get("pid_start")
                and _util.pid_start_ticks(int(item["pid"])) == item["pid_start"]
            )
            workers.append(item)

    controller_state = counts["meta"].get("controller_state", "unknown")
    stop_requested = (counts["meta"].get("stop_requested", "0") == "1")

    attention: list[str] = []
    if controller_state == "attention":
        attention.append("controller_state=attention（worker 重启超限或异常退出）")
    if any(worker["state"] == "error" for worker in workers):
        attention.append("存在 error 状态的 worker")
    export_status = counts["meta"].get("export_status")
    if export_status and export_status.startswith("ATTENTION"):
        attention.append(f"export_status={export_status}")
    missing_results = pool.verify_done_results_exist()
    if missing_results:
        attention.append(f"{len(missing_results)} 个已完成 slot 的结果文件缺失")

    disk_free_gb = None
    try:
        import shutil as _shutil

        disk_free_gb = round(_shutil.disk_usage(cfg.run_scope).free / (1024**3), 1)
    except OSError:
        pass

    return {
        "scope": cfg.scope,
        "exp": cfg.exp,
        "round_number": cfg.round_number,
        "mode": cfg.stage_policy.mode,
        "stage_policy": cfg.stage_policy.to_manifest(),
        "controller_state": controller_state,
        "stop_requested": stop_requested,
        "supervisor": {
            "alive": alive,
            "reason": alive_reason,
            "pid_file": pid_payload,
        },
        "tasks": counts["tasks"],
        "slots": counts["slots"],
        "outcome_summary": outcome_summary,
        "workers": workers,
        "attention": attention,
        "disk_free_gb": disk_free_gb,
        "missing_results": missing_results[:20],
    }

class _closing:
    def __init__(self, obj):
        self._obj = obj

    def __enter__(self):
        return self._obj

    def __exit__(self, *_exc):
        self._obj.close()
        return False

def render_status_text(payload: dict[str, Any]) -> str:
    """终端友好的状态渲染。"""
    lines = []
    state = payload["controller_state"].upper()
    if payload["stop_requested"] and state == "RUNNING":
        state = "STOPPING"
    lines.append(f"=== CyberGym-E2E {payload['exp']} round {payload['round_number']} "
                 f"[{state}] scope={payload['scope']} ===")
    lines.append(
        f"MODE={payload['mode']} enabled={payload['stage_policy']['enabled_stages']} "
        f"success={payload['stage_policy']['success_stages']}"
        + (" (custom profile)" if not payload["stage_policy"].get("default_profile", True) else "")
    )
    summary = payload["outcome_summary"]
    lines.append(
        f"tasks: done={payload['tasks'].get('done', 0)} running={payload['tasks'].get('running', 0)} "
        f"pending={payload['tasks'].get('pending', 0)} / total={summary['tasks_total']}"
    )
    lines.append(
        f"repeats: finished={summary['finished_repeats']}/{summary['planned_repeats']} "
        f"running={payload['slots'].get('running', 0)} pending={payload['slots'].get('pending', 0)} "
        f"abandoned_attempts={summary['abandoned_attempts']}"
    )
    outcomes = summary["outcomes"]
    lines.append(
        "outcomes: " + " ".join(f"{k}={v}" for k, v in sorted(outcomes.items()))
        if outcomes else "outcomes: (none yet)"
    )
    lines.append(
        f"valid results: {summary['valid_results']} "
        f"success_rate={summary['valid_success_rate']:.4f} "
        f"(tasks any-success={summary['tasks_any_success']})"
    )
    lines.append(
        f"throughput={summary['throughput_per_second']:.6f}/s "
        f"eta={summary['eta_seconds'] if summary['eta_seconds'] is not None else 'unknown'}s"
    )
    if payload["workers"]:
        lines.append("workers:")
        for worker in payload["workers"]:
            current = ""
            if worker.get("task_path"):
                current = f" task={worker['task_path']} r{worker.get('current_repeat')}"
                if worker.get("attempt_phase"):
                    current += f" phase={worker['attempt_phase']}"
            lines.append(
                f"  rank={worker['rank']} state={worker['state']} alive={worker['alive']} "
                f"restarts={worker['restarts']}{current}"
                + (f" error={worker['error']}" if worker.get("error") else "")
            )
    if payload.get("disk_free_gb") is not None:
        lines.append(f"disk free: {payload['disk_free_gb']} GiB")
    for attention in payload["attention"]:
        lines.append(f"ATTENTION: {attention}")
    return "\n".join(lines)
