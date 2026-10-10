"""离线汇总（07）：capture drain、结果适配、关联轨迹、完整性分析与 ShareGPT。

要求队列 COMPLETE（或显式 --snapshot）、无活动 worker/attempt，并等待
本轮相关 capture 收尾。按所有 launch 聚合。已有产物不原地覆盖——重复
执行使用版本目录；按输入 hash 和导出 policy 保持幂等。

评分成功和轨迹可导出分别统计：模型成功但日志缺失不改为评测失败，
日志完整也不等于任务成功。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import util
from .config import ShellConfig
from .reports import summarize_outcomes

def wait_capture_drain(capture_launch_dir: Path, timeout_seconds: float) -> bool:
    """等待 launch 的 inflight 请求收尾；超时保留审计并继续归档。"""
    inflight = Path(capture_launch_dir) / "raw" / "inflight"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not inflight.exists() or not any(inflight.iterdir()):
            return True
        time.sleep(5)
    return not (inflight.exists() and any(inflight.iterdir()))

def _ensure_no_active_workers(cfg: ShellConfig) -> None:
    if cfg.queue_db_path().exists():
        from .queue import TaskPool

        pool = TaskPool(cfg.queue_db_path())
        for attempt in pool.active_attempts():
            raise RuntimeError(f"attempt 仍在活动状态: {attempt['attempt_id']}")
    from .supervisor import supervisor_alive

    alive, reason = supervisor_alive(cfg)
    if alive:
        raise RuntimeError("supervisor 仍在运行；请先等待或执行 08_stop.sh")

def _next_version_dir(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    number = 1
    while (base / f"v{number}").exists():
        number += 1
    return base / f"v{number}"

def finalize_and_summarize(cfg: ShellConfig, *, allow_incomplete: bool = False) -> dict[str, Any]:
    """07 主流程；返回汇总报告。"""
    _ensure_no_active_workers(cfg)
    from .queue import TaskPool

    pool = TaskPool(cfg.queue_db_path())
    if not allow_incomplete and not pool.is_complete():
        raise RuntimeError("队列未 COMPLETE；使用 --snapshot 进行部分汇总")

    summary_dir = _next_version_dir(cfg.run_scope / "summary")
    trajectory_input = summary_dir / "trajectory_input"
    capture_aggregate = summary_dir / "capture_aggregate"
    trajectory_output = summary_dir / "trajectory_dataset"
    integrity_output = summary_dir / "trajectory_integrity"
    sharegpt_output = summary_dir / "sharegpt"

    # 1) capture 聚合视图（不可变；来源索引）
    from .trajectory_adapter import aggregate_capture_logs, build_attempt_view

    aggregate_report = aggregate_capture_logs(cfg.run_scope, capture_aggregate)

    # 2) attempt 视图（result.json 主 checks 表达当前成功策略）
    input_report = build_attempt_view(
        cfg.queue_db_path(), trajectory_input,
        stage_success_policy=cfg.stage_policy.success_stages,
    )

    # 3) 汇总（分母分离）
    outcome_summary = summarize_outcomes(cfg)

    # 4) finalizer：session ↔ capture 以 message.id 关联
    finalizer = cfg.capture_proxy_dir / "finalize-proc-split.py"
    finalizer_report: dict[str, Any] = {"skipped": "finalizer_missing"}
    if finalizer.exists():
        result = subprocess.run(
            [sys.executable, str(finalizer),
             "--capture-dir", str(capture_aggregate / "raw"),
             "--proc-dir", str(trajectory_input),
             "--output-dir", str(trajectory_output)],
            capture_output=True, text=True, timeout=86400,
        )
        util.atomic_write_json(summary_dir / "finalizer.stdout.json", {
            "returncode": result.returncode,
            "stdout_tail": result.stdout[-20000:],
            "stderr_tail": result.stderr[-20000:],
        })
        if result.returncode == 0 and (trajectory_output / "finalization_report.json").exists():
            finalizer_report = util.read_json(trajectory_output / "finalization_report.json")
        else:
            finalizer_report = {"error": f"finalizer exit={result.returncode}"}

    # 5) 完整性分析（只读；报告不反过来改写评分）
    traj_analysis = Path("/data/cybergym/glm_5_1_scripts/traj_analysis.py")
    integrity_report: dict[str, Any] = {"skipped": "traj_analysis_missing"}
    if traj_analysis.exists() and trajectory_input.is_dir():
        groups = [d for d in trajectory_input.iterdir() if d.is_dir()]
        result = subprocess.run(
            [sys.executable, str(traj_analysis), *[str(g) for g in groups],
             "-o", str(integrity_output)],
            capture_output=True, text=True, timeout=7200,
        )
        if result.returncode == 0 and (integrity_output / "summary.json").exists():
            integrity_report = util.read_json(integrity_output / "summary.json")
        else:
            integrity_report = {"error": f"traj_analysis exit={result.returncode}"}

    # 6) ShareGPT 导出
    sharegpt_report: dict[str, Any] = {"enabled": cfg.export_sharegpt}
    if cfg.export_sharegpt:
        exporter = cfg.capture_proxy_dir / "export_sharegpt_recovered.py"
        for outcome in ("successful", "failed"):
            source = trajectory_output / outcome
            if not source.is_dir():
                continue
            target = sharegpt_output / outcome
            result = subprocess.run(
                [sys.executable, str(exporter),
                 "--input-dir", str(source), "--output-dir", str(target),
                 "--reasoning-mode", "separate"],
                capture_output=True, text=True, timeout=86400,
            )
            report_path = sharegpt_output / f"{outcome}_export_report.json"
            if report_path.exists():
                sharegpt_report[outcome] = util.read_json(report_path)
            else:
                sharegpt_report[outcome] = {"error": f"exit={result.returncode}"}

    # 7) 总报告：评分、采集覆盖率、可导出轨迹数量使用各自分母
    totals = {
        "planned_repeats": outcome_summary["planned_repeats"],
        "finished_repeats": outcome_summary["finished_repeats"],
        "valid_results": outcome_summary["valid_results"],
        "exportable_trajectories": input_report.get("attempts_with_sessions", 0),
        "captures_aggregated": aggregate_report.get("captures", 0),
        "denominators_note": (
            "planned=计划 repeat；finished=已完成 repeat；valid=有确定判定；"
            "exportable=有 session 日志；captures=聚合 capture 数；互不通用"
        ),
    }
    payload = {
        "schema_version": 1,
        "generated_at": util.utc_now_iso(),
        "scope": cfg.scope,
        "summary_dir": str(summary_dir),
        "stage_policy": cfg.stage_policy.to_manifest(),
        "outcome_summary": outcome_summary,
        "capture_aggregation": aggregate_report,
        "trajectory_input": input_report,
        "finalizer": finalizer_report,
        "integrity": integrity_report,
        "sharegpt": sharegpt_report,
        "totals": totals,
        "snapshot": bool(allow_incomplete),
    }
    util.atomic_write_json(summary_dir / "postprocess_summary.json", payload)
    _write_summary_md(summary_dir / "summary.md", payload)
    return payload

def _write_summary_md(path: Path, payload: dict[str, Any]) -> None:
    outcome = payload["outcome_summary"]
    totals = payload["totals"]
    lines = [
        "# CyberGym-E2E 汇总",
        "",
        f"- 模式: {payload['stage_policy']['mode']}；启用阶段: {payload['stage_policy']['enabled_stages']}"
        f"；成功条件: {payload['stage_policy']['success_stages']}"
        f"{'（custom profile）' if payload['stage_policy'].get('default_profile') is False else ''}",
        f"- 任务: {outcome['tasks_done']}/{outcome['tasks_total']}",
        f"- repeat: {outcome['finished_repeats']}/{outcome['planned_repeats']}"
        f"（abandoned={outcome['abandoned_attempts']}）",
        f"- 有效结果: success={outcome['outcomes'].get('success', 0)} "
        f"failed={outcome['outcomes'].get('failed', 0)} "
        f"indeterminate={outcome['outcomes'].get('indeterminate', 0)} "
        f"infra_error={outcome['outcomes'].get('infra_error', 0)}",
        f"- 有效结果成功率: {outcome['valid_success_rate']:.4f}"
        f"（分母=有确定判定的 repeat {outcome['valid_results']}）",
        f"- 任一 repeat 成功的 task: {outcome['tasks_any_success']}/{outcome['tasks_total']}",
        f"- 可导出轨迹: {totals['exportable_trajectories']}（与评分分母分开统计）",
        f"- 聚合 capture: {totals['captures_aggregated']}（冲突 {len(payload['capture_aggregation'].get('conflicts', []))}）",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
