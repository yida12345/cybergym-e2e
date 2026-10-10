"""重新分组（09）：只使用已保存的阶段证据重新计算成功条件。

不运行模型或验证，不改写原始评分（queue、result.json、checks 均保持
原样）。输出到独立 policy 目录 regroup/<policy_hash>/。

新条件引用 disabled／未执行阶段时给出不可确定及覆盖率，不伪造失败，
不因重分组覆盖旧训练数据或制造重复样本。
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from . import util
from .config import ShellConfig
from .results import stage_description


def reclassify(
    cfg: ShellConfig,
    new_success_stages: list[int],
    *,
    online_root: Path | None = None,
) -> dict[str, Any]:
    """按新的 SUCCESS_STAGES 重新分组；返回报告并写入 policy 目录。

    - 阶段证据来自 stage_results 表（每个 finished attempt 的独立阶段
      normalized_status）；
    - 引用未执行/disabled 阶段 → not_evaluable + 覆盖率；
    - 评分与轨迹可导出分别统计；不触碰已有导出目录。
    """
    from .queue import TaskPool, connect

    pool = TaskPool(cfg.queue_db_path())
    with contextlib.closing(connect(cfg.queue_db_path(), readonly=True)) as db:
        attempts = [
            dict(row)
            for row in db.execute(
                "SELECT a.attempt_id,a.task_id,a.repeat_index,a.generation,a.outcome,"
                "a.output_dir,a.receipt_hash,t.task_path FROM attempts a "
                "JOIN tasks t ON t.id=a.task_id WHERE a.state='finished' ORDER BY a.id"
            )
        ]
    stage_results = pool.stage_results([a["attempt_id"] for a in attempts])

    records: list[dict[str, Any]] = []
    counts = {"success": 0, "failed": 0, "indeterminate": 0, "not_evaluable": 0}
    coverage: dict[int, dict[str, int]] = {}
    for attempt in attempts:
        stages = stage_results.get(attempt["attempt_id"], {})
        stage_records = {}
        for stage, row in stages.items():
            stage_records[stage] = row["normalized_status"]
        evaluation = _evaluate(stage_records, new_success_stages)
        evaluation["original_outcome"] = attempt["outcome"]
        evaluation["attempt_id"] = attempt["attempt_id"]
        evaluation["task"] = attempt["task_path"]
        evaluation["repeat_index"] = attempt["repeat_index"]
        evaluation["generation"] = attempt["generation"]
        records.append(evaluation)
        counts[evaluation["configured_success"]] = counts.get(evaluation["configured_success"], 0) + 1
        for stage in new_success_stages:
            status = stage_records.get(stage, "disabled")
            bucket = coverage.setdefault(stage, {})
            bucket[status] = bucket.get(status, 0) + 1

    policy_dir = cfg.run_scope / "regroup" / util.scope_hash(
        "regroup", ",".join(map(str, new_success_stages))
    )
    policy_dir.mkdir(parents=True, exist_ok=True)
    non_evaluable = [r for r in records if r["configured_success"] == "not_evaluable"]
    report = {
        "schema_version": 1,
        "generated_at": util.utc_now_iso(),
        "new_success_stages": list(new_success_stages),
        "original_policy": cfg.stage_policy.to_manifest(),
        "counts": counts,
        "stage_coverage": {str(k): v for k, v in coverage.items()},
        "not_evaluable_count": len(non_evaluable),
        "not_evaluable_examples": [
            {"attempt_id": r["attempt_id"], "stages": r["stage_statuses"]}
            for r in non_evaluable[:50]
        ],
        "records": records,
    }
    util.atomic_write_json(policy_dir / "reclassification.json", report)
    _write_jsonl(policy_dir / "reclassification.jsonl", records)
    _write_summary(policy_dir / "summary.md", report)
    return {"policy_dir": str(policy_dir), **{k: report[k] for k in
            ("counts", "stage_coverage", "not_evaluable_count", "new_success_stages")}}


def _evaluate(stage_records: dict[int, str], success_stages: list[int]) -> dict[str, Any]:
    statuses: dict[int, str] = {}
    indeterminate: list[int] = []
    non_evaluable: list[int] = []
    for stage in success_stages:
        status = stage_records.get(stage)
        if status is None or status == "disabled":
            statuses[stage] = status or "disabled"
            non_evaluable.append(stage)
        else:
            statuses[stage] = status
            if status in ("error", "interrupted"):
                indeterminate.append(stage)
    if non_evaluable:
        result = "not_evaluable"
    elif indeterminate:
        result = "indeterminate"
    elif all(statuses[s] == "passed" for s in success_stages):
        result = "success"
    else:
        result = "failed"
    return {
        "configured_success": result,
        "stage_statuses": {str(k): v for k, v in statuses.items()},
        "indeterminate_stages": sorted(indeterminate),
        "non_evaluable_stages": sorted(non_evaluable),
        "reason": _not_evaluable_reason(non_evaluable),
    }


def _not_evaluable_reason(non_evaluable: list[int]) -> str | None:
    if not non_evaluable:
        return None
    descriptions = ", ".join(f"S{stage}（{stage_description(stage)}）" for stage in non_evaluable)
    return (
        f"新成功条件引用了未执行或禁用的阶段: {descriptions}；"
        "disabled 阶段没有可恢复的通过信息，需要新的评测才能获得"
    )


def _write_summary(path: Path, report: dict[str, Any]) -> None:
    counts = report["counts"]
    total = sum(counts.values()) or 1
    lines = [
        "# 重新分组结果",
        "",
        f"- 新成功条件: {report['new_success_stages']}",
        f"- 原始策略: {report['original_policy']['mode']} "
        f"enabled={report['original_policy']['enabled_stages']} "
        f"success={report['original_policy']['success_stages']}",
        f"- success={counts.get('success', 0)} failed={counts.get('failed', 0)} "
        f"indeterminate={counts.get('indeterminate', 0)} not_evaluable={counts.get('not_evaluable', 0)}",
        f"- 覆盖率: " + json.dumps(report["stage_coverage"], ensure_ascii=False),
        f"- 不可评估: {report['not_evaluable_count']}（引用 disabled/未执行阶段）",
        "",
        "原始评分未被修改；本目录只包含新分组视图。",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
