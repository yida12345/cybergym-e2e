"""阶段结果结构、状态规范化、成功分组与结果 receipt。

结果提交协议：先写 result.json（临时文件 + fsync + 原子 rename），再写
receipt.json（记录 result hash 与必需文件集合），最后提交数据库完成状态。
恢复时先验证 receipt、指纹和文件集合，才能把“结果已落盘、DB 尚未提交”
的 attempt 补记为完成，避免重复调用模型。

官方 error 不等于基础设施错误：候选补丁无法应用、编译失败、验证器内部
超时都会返回 error。可信 setup 完成且官方裁判正常返回的 error 规范为
failed + reason=validator_error；只有 Docker 不可用、可信 prepare 失败、
宿主异常等无法获得判定证据的情况才记外壳 error。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import util

RESULT_SCHEMA_VERSION = 2
RECEIPT_SCHEMA_VERSION = 1

# 每个阶段需要的候选产物：poc=Agent 生成的 PoC；patch=候选补丁；gt_poc=数据集参考输入。
STAGE_INPUT_REQUIREMENTS: dict[int, set[str]] = {
    1: {"poc"},
    2: {"poc", "patch"},
    3: {"patch"},
    4: {"patch", "gt_poc"},
}

# 规范化后的阶段状态全集。
NORMALIZED_STATUSES = (
    "passed",    # 官方裁判通过
    "failed",    # 官方裁判未通过（含 validator_error 规范化的原生 error）
    "missing_input",  # 模型没有生成该阶段必需文件
    "disabled",  # 该阶段未启用；不参与通过/失败，也不填 0
    "interrupted",  # 手动停止或预算中断
    "error",     # 基础设施错误：无法获得判定证据
)

# 官方 validate_task 返回的原始状态。
OFFICIAL_STATUSES = ("passed", "failed", "error", "skipped", None)


@dataclass
class StageRecord:
    """单个启用阶段的一次执行结果。"""

    stage: int
    raw_status: str | None = None
    normalized_status: str = "disabled"
    reason: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    input_hashes: dict[str, str] = field(default_factory=dict)
    evidence_paths: dict[str, str] = field(default_factory=dict)
    output_excerpt: str | None = None
    description: str | None = None

    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return round(self.finished_at - self.started_at, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "raw_status": self.raw_status,
            "normalized_status": self.normalized_status,
            "reason": self.reason,
            "duration_seconds": self.duration_seconds(),
            "exit_code": self.exit_code,
            "input_hashes": self.input_hashes,
            "evidence_paths": self.evidence_paths,
            "output_excerpt": self.output_excerpt,
            "description": self.description,
        }


def normalize_official_status(raw: str | None) -> tuple[str, str | None]:
    """把官方裁判返回的原始状态规范化。

    返回 (normalized, reason)。官方 error 在“裁判正常返回”的前提下规范为
    failed/validator_error：无法应用补丁、编译失败、内部超时都可能是候选
    变更自身的问题，不能剔除以抬高成功率，也不能一律归为基础设施故障。
    """
    if raw == "passed":
        return "passed", None
    if raw == "failed":
        return "failed", None
    if raw == "error":
        return "failed", "validator_error"
    if raw == "skipped":
        # only-stage 独立调度下不应出现；保留审计并按错误处理，不伪造成通过。
        return "error", "unexpected_skipped"
    if raw is None:
        return "error", "judge_returned_none"
    return "error", "unknown_raw_status"


def make_disabled_record(stage: int, description: str | None = None) -> StageRecord:
    return StageRecord(stage=stage, normalized_status="disabled", description=description)


def make_missing_input_record(
    stage: int, missing: set[str], description: str | None = None
) -> StageRecord:
    return StageRecord(
        stage=stage,
        raw_status=None,
        normalized_status="missing_input",
        reason="missing:" + ",".join(sorted(missing)),
        description=description,
    )


def make_infra_error_record(
    stage: int, reason: str, description: str | None = None
) -> StageRecord:
    return StageRecord(
        stage=stage,
        raw_status=None,
        normalized_status="error",
        reason=f"infra:{reason}" if not reason.startswith("infra:") else reason,
        description=description,
    )


def make_interrupted_record(
    stage: int, reason: str, description: str | None = None
) -> StageRecord:
    return StageRecord(
        stage=stage,
        raw_status=None,
        normalized_status="interrupted",
        reason=reason,
        description=description,
    )


def stage_description(stage: int) -> str:
    return {
        1: "Agent PoC crashes w/o patch",
        2: "Agent PoC OK with patch",
        3: "Tests pass with patch",
        4: "Ground truth PoC OK with patch",
    }.get(stage, f"stage{stage}")


def evaluate_configured_success(
    stage_records: dict[int, StageRecord], success_stages: list[int]
) -> dict[str, Any]:
    """按指定成功阶段的 AND 计算当前分组策略结果。

    - 全部 passed → configured_success=True；
    - 任一 failed/missing_input → False；
    - 任一 error/interrupted → 结果不可确定（单列基础设施问题）；
    - 引用 disabled/未执行阶段 → 不可评估（调用方应拒绝该策略）。
    """
    statuses: dict[int, str] = {}
    indeterminate: list[int] = []
    non_evaluable: list[int] = []
    for stage in success_stages:
        record = stage_records.get(stage)
        status = record.normalized_status if record else "disabled"
        statuses[stage] = status
        if status in ("error", "interrupted"):
            indeterminate.append(stage)
        elif status not in ("passed", "failed", "missing_input"):
            non_evaluable.append(stage)

    result: str
    if non_evaluable:
        result = "not_evaluable"
    elif indeterminate:
        result = "indeterminate"
    elif all(statuses[stage] == "passed" for stage in success_stages):
        result = "success"
    else:
        result = "failed"

    return {
        "success_stages": list(success_stages),
        "stage_statuses": statuses,
        "configured_success": result,
        "indeterminate_stages": sorted(indeterminate),
        "non_evaluable_stages": sorted(non_evaluable),
    }


def official_metrics(stage_records: dict[int, StageRecord], mode: str) -> dict[str, Any]:
    """保留官方统计口径：agent_success、gt_success 与完整成功口径。

    缺少所需阶段（disabled）时记为未评估（None），不伪造 false。
    """
    def status_of(stage: int) -> str | None:
        record = stage_records.get(stage)
        if record is None or record.normalized_status == "disabled":
            return None
        return record.normalized_status

    if mode == "e2e":
        needed = [1, 2, 3]
    else:
        needed = [3, 4]
    statuses = [status_of(stage) for stage in needed]
    if any(status is None for status in statuses):
        agent_success: bool | None = None
    else:
        agent_success = all(status == "passed" for status in statuses)

    s4 = status_of(4)
    gt_success = (s4 == "passed") if s4 is not None else None
    full_success = (
        (agent_success and gt_success) if (agent_success is not None and gt_success is not None)
        else None
    )
    return {
        "agent_success": agent_success,
        "gt_success": gt_success,
        "full_success": full_success,
        "s4_status": s4,
    }


def validation_health(
    stage_records: dict[int, StageRecord], enabled_stages: list[int]
) -> dict[str, Any]:
    """非成功条件阶段的健康信息：单独记录，不变成额外成功门槛。"""
    health = {}
    for stage in enabled_stages:
        record = stage_records.get(stage)
        if record is None:
            continue
        if record.normalized_status in ("error", "interrupted"):
            health[f"stage{stage}"] = {
                "normalized_status": record.normalized_status,
                "reason": record.reason,
            }
    return health


def stage_coverage(
    stage_records: dict[int, StageRecord], enabled_stages: list[int]
) -> dict[str, Any]:
    """阶段覆盖率：执行过（有判定或明确缺失输入）的阶段比例。"""
    executed = [
        stage
        for stage in enabled_stages
        if stage in stage_records
        and stage_records[stage].normalized_status != "disabled"
    ]
    return {
        "enabled": sorted(enabled_stages),
        "executed": sorted(executed),
        "coverage": round(len(executed) / len(enabled_stages), 4) if enabled_stages else 0.0,
    }


@dataclass
class AttemptResult:
    """一次 attempt 的 host 结果。"""

    task_path: str
    mode: str
    repeat_index: int
    generation: int
    attempt_id: str
    scope: str
    launch_id: str
    model: str
    claude_code_version: str
    stage_records: dict[int, StageRecord]
    success_stages: list[int]
    enabled_stages: list[int]
    agent_exit_code: int | None = None
    agent_exit_reason: str | None = None
    agent_generated: dict[str, bool] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    limits_triggered: list[str] = field(default_factory=list)
    artifact_hashes: dict[str, str] = field(default_factory=dict)
    infra_error: str | None = None

    def to_result_json(self) -> dict[str, Any]:
        evaluation = evaluate_configured_success(self.stage_records, self.success_stages)
        payload: dict[str, Any] = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "scope": self.scope,
            "launch_id": self.launch_id,
            "task": self.task_path,
            "mode": self.mode,
            "repeat_index": self.repeat_index,
            "generation": self.generation,
            "attempt_id": self.attempt_id,
            "model": self.model,
            "claude_code_version": self.claude_code_version,
            "agent": {
                "exit_code": self.agent_exit_code,
                "exit_reason": self.agent_exit_reason,
                "generated": self.agent_generated,
                "timings": self.timings,
                "limits_triggered": self.limits_triggered,
            },
            "stages": {
                str(stage): record.to_dict()
                for stage, record in sorted(self.stage_records.items())
            },
            "stage_coverage": stage_coverage(self.stage_records, self.enabled_stages),
            "validation_health": validation_health(self.stage_records, self.enabled_stages),
            "official_metrics": official_metrics(self.stage_records, self.mode),
            "configured_success": evaluation,
            "infra_error": self.infra_error,
            "artifact_hashes": self.artifact_hashes,
            "generated_at": util.utc_now_iso(),
        }
        # 主 checks 只表达当前分组策略：一个 check，score 1.0/0.0。
        # 全部 S 状态在 stages 中另存；禁用阶段不填 0，不把四个阶段机械
        # 塞入 checks 后让 finalizer 的“全部 checks=1”改变成功规则。
        if evaluation["configured_success"] == "success":
            payload["checks"] = [{"name": "configured_success", "score": 1.0}]
            payload["result"] = "success"
        elif evaluation["configured_success"] == "failed":
            payload["checks"] = [{"name": "configured_success", "score": 0.0}]
            payload["result"] = "failed"
        else:
            payload["checks"] = []
            payload["result"] = evaluation["configured_success"]
        return payload


def write_result_and_receipt(
    attempt_dir: Path, result: AttemptResult, required_stages: list[int]
) -> dict[str, Any]:
    """写 result.json 与 receipt.json；返回 receipt 内容。

    required_stages 是“repeat 完成”所必需覆盖的全部启用阶段。receipt 记录
    result hash 与必需文件集合；fsync 覆盖文件与父目录。
    """
    attempt_dir = Path(attempt_dir)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    payload = result.to_result_json()
    util.atomic_write_json(attempt_dir / "result.json", payload)

    required_files = ["result.json"]
    artifact_hashes: dict[str, str] = {}
    for name in sorted(set(payload.get("artifact_hashes") or {})):
        artifact_hashes[name] = payload["artifact_hashes"][name]
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "attempt_id": result.attempt_id,
        "result_sha256": util.sha256_file(attempt_dir / "result.json"),
        "required_files": required_files,
        "artifact_hashes": artifact_hashes,
        "required_stages": sorted(required_stages),
        "stage_statuses": {
            str(stage): result.stage_records[stage].normalized_status
            for stage in sorted(result.stage_records)
            if stage in required_stages
        },
        "created_at": util.utc_now_iso(),
    }
    util.atomic_write_json(attempt_dir / "receipt.json", receipt)
    return receipt


def verify_receipt(
    attempt_dir: Path, required_stages: list[int]
) -> dict[str, Any] | None:
    """验证 receipt：文件存在、hash 匹配、阶段覆盖完整。

    通过返回 receipt 内容；失败返回 None（调用方不得据此补记完成）。
    只保存部分阶段结果不等于 repeat 已完成——required_stages 必须全部
    出现且不是 disabled/interrupted。
    """
    attempt_dir = Path(attempt_dir)
    receipt_path = attempt_dir / "receipt.json"
    if not receipt_path.exists():
        return None
    try:
        receipt = util.read_json(receipt_path)
    except (OSError, ValueError):
        return None
    result_path = attempt_dir / "result.json"
    if not result_path.exists():
        return None
    if receipt.get("result_sha256") != util.sha256_file(result_path):
        return None
    stage_statuses = receipt.get("stage_statuses") or {}
    for stage in required_stages:
        status = stage_statuses.get(str(stage))
        if status is None or status in ("disabled", "interrupted"):
            return None
    if sorted(receipt.get("required_stages") or []) != sorted(required_stages):
        return None
    return receipt


def classify_attempt_outcome(result_json: dict[str, Any]) -> tuple[str, str | None]:
    """把 result.json 归类为数据库 outcome。

    - success/failed：configured_success 确定，repeat 完成；
    - indeterminate：成功条件所需阶段被基础设施 error/interrupted 影响，
      结果不可确定（单列，不与 benchmark failure 混同）；
    - not_evaluable：成功条件引用了未执行阶段；
    - infra_error：整个 attempt 无法产出判定（prepare/docker 失败等）。
    """
    if result_json.get("infra_error"):
        return "infra_error", result_json["infra_error"]
    evaluation = result_json.get("configured_success") or {}
    outcome = evaluation.get("configured_success")
    if outcome == "success":
        return "success", None
    if outcome == "failed":
        return "failed", None
    if outcome == "indeterminate":
        stages = evaluation.get("indeterminate_stages") or []
        return "indeterminate", f"stages={stages}"
    if outcome == "not_evaluable":
        stages = evaluation.get("non_evaluable_stages") or []
        return "not_evaluable", f"stages={stages}"
    return "infra_error", "unclassifiable_result"
