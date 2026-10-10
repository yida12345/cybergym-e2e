"""轨迹输入适配：capture 聚合视图与 attempt 目录布局。

两件事：
1. capture 聚合：跨 launch 的原始采集不能直接把某个 launch 目录当 raw
   根（现有 finalizer 扫描 raw/completed 的直接子目录）。这里建立不可
   变聚合视图，把已收尾 capture 按原始 capture_id 放入
   ``capture_raw/raw/completed/<capture_id>``，同 ID 同内容去重，不同
   内容保留冲突（不覆盖），另存来源 launch 与 hash 索引。
2. attempt 布局：适配 finalizer 的 ``<group>/<task_id>`` proc 结构与
   traj_analysis 的 ``logs/<样本>/console.log``：
   ``<group>/<attempt_key>/result.json``（主 checks 只表达当前成功策略）、
   ``logs/projects/**/*.jsonl``（CC session）、``logs/<attempt_key>/console.log``。

关联依据（capture message_id ↔ session assistant message.id）由现有
finalizer 完成；本模块只建立布局，不用时间相近猜测。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from . import util

CAPTURE_FILES = ("request.json", "response.json", "state.json", "sse_events.jsonl", "response.body")
EXPORTABLE_OUTCOMES = ("success", "failed")


def attempt_key(task_id: int, task_path: str, repeat_index: int, generation: int, attempt_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", task_path).strip("_")
    return f"{int(task_id):06d}-{safe}__r{repeat_index:03d}__g{generation:03d}__{attempt_id}"


# ---------------------------------------------------------------------------
# capture 聚合
# ---------------------------------------------------------------------------


def aggregate_capture_logs(run_scope: Path, output_root: Path) -> dict[str, Any]:
    """把所有 launch 的已收尾 capture 聚合成 finalizer 兼容布局。

    - 只聚合 completed（inflight/retry_attempts 保留在原 launch 目录，
      单独索引，不混入 raw/completed）；
    - 同 capture_id 同内容去重；不同内容记冲突，不覆盖；
    - 来源索引记录每个 capture_id 来自哪个 launch。
    """
    run_scope = Path(run_scope)
    capture_root_all = run_scope / "capture_logs"
    aggregate_root = Path(output_root)
    completed_root = aggregate_root / "raw" / "completed"
    inflight_index_path = aggregate_root / "inflight_index.jsonl"
    sources_index_path = aggregate_root / "capture_sources.jsonl"
    report: dict[str, Any] = {
        "launches": 0, "captures": 0, "duplicates": 0,
        "conflicts": [], "inflight_listed": 0,
    }

    seen: dict[str, str] = {}  # capture_id -> content hash
    inflight_records: list[dict[str, Any]] = []
    source_records: list[dict[str, Any]] = []

    if capture_root_all.is_dir():
        for launch_dir in sorted(capture_root_all.glob("launch_*")):
            if not launch_dir.is_dir():
                continue
            report["launches"] += 1
            raw = launch_dir / "raw"
            completed = raw / "completed"
            if completed.is_dir():
                for capture_dir in sorted(completed.iterdir()):
                    if not capture_dir.is_dir():
                        continue
                    capture_id = capture_dir.name
                    content_hash = _capture_content_hash(capture_dir)
                    if capture_id in seen:
                        if seen[capture_id] == content_hash:
                            report["duplicates"] += 1
                            source_records.append(
                                {"capture_id": capture_id, "launch": launch_dir.name, "duplicate": True}
                            )
                            continue
                        report["conflicts"].append(
                            {"capture_id": capture_id, "existing": seen[capture_id],
                             "new": content_hash, "launch": launch_dir.name}
                        )
                        # 冲突副本保留到 conflicts/，不覆盖已有内容
                        conflict_dir = aggregate_root / "conflicts" / f"{capture_id}-{launch_dir.name}"
                        if not conflict_dir.exists():
                            shutil.copytree(capture_dir, conflict_dir)
                        continue
                    seen[capture_id] = content_hash
                    destination = completed_root / capture_id
                    destination.mkdir(parents=True, exist_ok=True)
                    for name in CAPTURE_FILES:
                        source = capture_dir / name
                        if source.exists():
                            util.hardlink_or_copy(source, destination / name, mode="copy")
                    report["captures"] += 1
                    source_records.append(
                        {"capture_id": capture_id, "launch": launch_dir.name,
                         "content_hash": content_hash}
                    )
            inflight = raw / "inflight"
            if inflight.is_dir():
                for entry in sorted(inflight.iterdir()):
                    if entry.is_dir():
                        report["inflight_listed"] += 1
                        inflight_records.append(
                            {"capture_id": entry.name, "launch": launch_dir.name}
                        )
            retry_dir = raw / "retry_attempts"
            if retry_dir.is_dir():
                for group in sorted(retry_dir.iterdir()):
                    if group.is_dir():
                        for entry in sorted(group.iterdir()):
                            if entry.is_dir():
                                report["inflight_listed"] += 1
                                inflight_records.append(
                                    {"capture_id": entry.name, "launch": launch_dir.name,
                                     "kind": "retry_attempt"}
                                )

    _write_jsonl(sources_index_path, source_records)
    _write_jsonl(inflight_index_path, inflight_records)
    util.atomic_write_json(aggregate_root / "aggregation_report.json", report)
    return report


def _capture_content_hash(capture_dir: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    for name in CAPTURE_FILES:
        path = capture_dir / name
        if path.exists():
            digest.update(name.encode())
            digest.update(util.sha256_file(path).encode())
    return digest.hexdigest()


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# attempt 布局
# ---------------------------------------------------------------------------


def build_attempt_view(
    db_path: Path,
    output_root: Path,
    *,
    stage_success_policy: list[int],
    include_indeterminate: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    """按 finalizer 布局生成每个 finished attempt 的视图目录。

    - 主 checks 只表达当前分组策略（一个 check，1.0/0.0）；
    - 全部 S 状态在 result.json 的 stages 中另存；
    - indeterminate / infra / interrupted 默认不进入 successful/failed
      训练集合（include_indeterminate=True 时放 audit 组，仅供排查）。
    """
    db_path = Path(db_path)
    output_root = Path(output_root)
    if output_root.exists() and not overwrite:
        raise RuntimeError(f"轨迹输入目录已存在: {output_root}")
    staging = output_root.with_name(f".{output_root.name}.tmp-{os.getpid()}")
    if staging.exists():
        shutil.rmtree(staging)

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    catalog: list[dict[str, Any]] = []
    try:
        rows = list(
            db.execute(
                "SELECT a.*,t.task_path FROM attempts a JOIN tasks t ON t.id=a.task_id "
                "WHERE a.state='finished' ORDER BY t.id,a.repeat_index,a.id"
            )
        )
        for row in rows:
            outcome = row["outcome"]
            result_path = Path(row["output_dir"]) / "result.json"
            if not result_path.exists():
                catalog.append({"attempt_id": row["attempt_id"], "problem": "result_missing"})
                continue
            result_json = util.read_json(result_path)
            group = _group_for_outcome(outcome, result_json, include_indeterminate)
            if group is None:
                catalog.append(
                    {"attempt_id": row["attempt_id"], "problem": f"outcome_{outcome}_excluded"}
                )
                continue
            key = attempt_key(
                row["task_id"], row["task_path"], row["repeat_index"], row["generation"],
                row["attempt_id"],
            )
            task_dir = staging / group / key
            task_dir.mkdir(parents=True, exist_ok=True)
            # result.json：主 checks 表达当前成功策略（finalizer 以此分组）
            adapted = dict(result_json)
            adapted["checks"] = _policy_checks(result_json, stage_success_policy)
            util.atomic_write_json(task_dir / "result.json", adapted)
            # CC session：只导出必要会话内容，不复制凭据配置
            projects_source = Path(row["output_dir"]) / "cc_home" / "projects"
            sessions = 0
            if projects_source.is_dir():
                for session in sorted(projects_source.rglob("*.jsonl")):
                    relative = session.relative_to(projects_source)
                    destination = task_dir / "logs" / "projects" / relative
                    util.hardlink_or_copy(session, destination, mode="copy")
                    sessions += 1
            # console.log → traj_analysis 识别的 logs/<样本>/console.log
            console_source = Path(row["output_dir"]) / "logs" / "console.log"
            if console_source.exists():
                util.hardlink_or_copy(
                    console_source, task_dir / "logs" / key / "console.log", mode="copy"
                )
            metadata = {
                "queue_task_id": row["task_id"],
                "attempt_id": row["attempt_id"],
                "trajectory_task_key": key,
                "task": row["task_path"],
                "repeat_index": row["repeat_index"],
                "generation": row["generation"],
                "outcome": outcome,
                "source_output_dir": row["output_dir"],
                "session_files": sessions,
                "launch_id": row["launch_id"],
            }
            util.atomic_write_json(task_dir / "attempt_metadata.json", metadata)
            catalog.append(metadata)
    finally:
        db.close()

    report = {
        "schema_version": 1,
        "attempts": len(catalog),
        "attempts_with_sessions": sum(1 for item in catalog if item.get("session_files")),
        "excluded": [item for item in catalog if "problem" in item],
        "catalog": catalog,
        "success_stages_policy": list(stage_success_policy),
    }
    util.atomic_write_json(staging / "input_report.json", report)
    staging.rename(output_root)
    return report


def _group_for_outcome(outcome: str, result_json: dict[str, Any], include_indeterminate: bool) -> str | None:
    if outcome == "success":
        return "successful"
    if outcome == "failed":
        return "failed"
    if include_indeterminate and outcome in ("indeterminate", "infra_error", "not_evaluable"):
        return "audit"
    return None


def _policy_checks(result_json: dict[str, Any], success_stages: list[int]) -> list[dict[str, Any]]:
    """主 checks：仅当前成功策略。indeterminate 不给分数（finalizer 跳过）。"""
    configured = (result_json.get("configured_success") or {}).get("configured_success")
    if configured == "success":
        return [{"name": "configured_success", "score": 1.0}]
    if configured == "failed":
        return [{"name": "configured_success", "score": 0.0}]
    return []
