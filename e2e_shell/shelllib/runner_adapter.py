"""runner_adapter.run_attempt(context)：单次独立执行的显式编排。

不直接在原生 batch_run 外套循环：内部使用一次独立执行（max_attempts=1
的语义），不同 repeat 不共享会话、候选产物或反馈，也不调用没有后续
用途的轨迹摘要模型（原生 feedback 构建在 max_attempts 检查之前，这里
显式省去）。

流程：镜像获取 → agent 容器（安装网络）→ 官方顺序 workspace 准备
（可信 prepare + 快照）→ CC 安装与会话目录 → 收窄网络 → CC 推理
（持续日志）→ 产物与会话导出 → 结束 Agent 容器 → 每个启用阶段新容器
独立验证 → 结果落盘 + receipt。

外层总预算与官方内部命令限额分别记录（config.total_attempt_budget_seconds
与 workspace.PREPARE_TIMEOUT 等）。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import util
from .cc_runner import CcEnvironment, CcRunner
from .config import ShellConfig
from .docker_control import ContainerSpec, DockerControl, DockerControlError
from .image_manager import ImageManager
from .prompts import OfficialPrompts
from .results import (
    AttemptResult,
    STAGE_INPUT_REQUIREMENTS,
    StageRecord,
    classify_attempt_outcome,
    make_disabled_record,
    make_infra_error_record,
    make_interrupted_record,
    make_missing_input_record,
    normalize_official_status,
    stage_description,
    write_result_and_receipt,
)
from .workspace import WorkspaceInputs, WorkspaceSetup

StopCheck = Callable[[], str | None]


class AttemptAborted(RuntimeError):
    """attempt 被停止/预算中断；保留已落盘证据。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class AttemptOutcome:
    outcome: str  # success / failed / indeterminate / not_evaluable / infra_error / interrupted
    error: str | None
    attempt_dir: Path
    result_json: dict[str, Any] | None = None


@dataclass
class AttemptContext:
    """一次 attempt 的全部依赖。"""

    cfg: ShellConfig
    manifest: dict[str, Any]
    docker: DockerControl
    images: ImageManager
    launch_id: str
    scope: str
    task_path: str
    mode: str
    repeat_index: int
    generation: int
    attempt_id: str
    attempt_dir: Path
    network_info: dict[str, Any]  # internal/install 网络、squid IP、capture 地址
    stop_check: StopCheck
    api_key: str
    prompt_hashes: dict[str, str] | None = None
    stage_timings: dict[str, float] = field(default_factory=dict)

    def check_stop(self) -> None:
        reason = self.stop_check()
        if reason:
            raise AttemptAborted(reason)

    @property
    def task_manifest_entry(self) -> dict[str, Any]:
        for task in self.manifest.get("tasks", []):
            if task["task_path"] == self.task_path:
                return task
        raise KeyError(f"manifest 中没有任务 {self.task_path}")


def run_attempt(context: AttemptContext) -> AttemptOutcome:
    """执行一次完整 attempt：Agent 推理 + 每启用阶段独立验证 + 结果落盘。

    返回 AttemptOutcome；数据库提交由调用方（supervisor worker）在
    receipt 落盘之后完成。
    """
    cfg = context.cfg
    entry = context.task_manifest_entry
    stage_policy = cfg.stage_policy
    attempt_dir = Path(context.attempt_dir)
    (attempt_dir / "logs").mkdir(parents=True, exist_ok=True)
    (attempt_dir / "output").mkdir(parents=True, exist_ok=True)

    stage_records: dict[int, StageRecord] = {}
    agent_exit_code: int | None = None
    agent_exit_reason: str | None = None
    agent_generated = {"poc": False, "patch": False}
    limits: list[str] = []
    infra_error: str | None = None
    timings: dict[str, float] = {}

    # 1) 镜像获取（含 lease）
    image = entry["build_image_acr"]
    image_info = context.images.ensure_image(
        image, scope=context.scope, launch_id=context.launch_id, attempt_id=context.attempt_id
    )

    # 2) Agent 容器：安装网络 + 共享 CC runtime 只读挂载 + 独立会话目录
    session_dir = attempt_dir / "cc_home"
    session_dir.mkdir(parents=True, exist_ok=True)
    agent_name = f"cg-e2e-agent-{context.attempt_id}"[:63]
    agent_spec = ContainerSpec(
        image=image,
        name=agent_name,
        role="agent",
        scope=context.scope,
        attempt_id=context.attempt_id,
        launch_id=context.launch_id,
        network=context.network_info["install_network"],
        workdir="/src",
        env={},
        memory=cfg.agent_memory,
        cpus=cfg.agent_cpus,
        pids_limit=cfg.agent_pids_limit,
        core_limit_bytes=cfg.agent_core_limit_bytes,
        volumes={
            str(cfg.cc_runtime_dir): {"bind": "/tools", "mode": "ro"},
            str(session_dir): {"bind": "/cc-home", "mode": "rw"},
        },
    )
    workspace_inputs = WorkspaceInputs(
        task_path=context.task_path,
        mode=context.mode,
        frozen_dir=cfg.frozen_dir,
        repo_dir=cfg.repo_dir,
        build_image_source=entry["build_image_source"],
        copy_gt_poc=False,  # agent 不接触参考 PoC
    )

    prompts = OfficialPrompts(cfg.repo_dir, context.prompt_hashes)
    cc_runner = CcRunner(context.docker)
    lease_released = False
    try:
        setup_started = time.monotonic()
        context.images.refresh_lease_pid(
            image, context.scope, context.launch_id, context.attempt_id, os.getpid()
        )
        agent_info = context.docker.create_and_start(agent_spec)
        agent_container = agent_info["Id"]
        (attempt_dir / "logs" / "agent_container.json").write_text(
            _json_dump(agent_info), encoding="utf-8"
        )

        # 3) 官方顺序的 workspace 准备（可信 prepare + /src_backup 快照）
        setup = WorkspaceSetup(context.docker, workspace_inputs)
        setup.setup(agent_container, run_prepare=True)
        timings["agent_setup"] = round(time.monotonic() - setup_started, 3)
        if time.monotonic() - setup_started > cfg.agent_setup_timeout_seconds:
            raise AttemptAborted("agent_setup_budget_exceeded")

        # 4) CC 安装（共享 runtime + 兼容性检查）与会话目录权限
        cc_env = CcEnvironment(
            base_url=context.network_info["capture_base_url"],
            api_key=context.api_key,
            model=cfg.model,
            reasoning_effort=cfg.reasoning_effort,
            api_timeout_ms=cfg.agent_api_timeout_ms,
            api_max_retries=cfg.agent_api_max_retries,
            max_output_tokens=cfg.agent_max_output_tokens,
            result_idle_timeout=cfg.agent_result_idle_timeout_seconds,
            force_idle_timeout=cfg.agent_api_force_idle_timeout,
            squid_url=context.network_info.get("squid_url"),
        )
        runtime_info = cc_runner.install_shared_runtime(
            agent_container, cc_env, node_bin="/tools/node/bin/node"
        )
        uid, gid = cc_runner.agent_uid(agent_container)
        _chown_recursive(session_dir, uid, gid)
        (attempt_dir / "logs" / "cc_runtime.json").write_text(
            _json_dump(runtime_info), encoding="utf-8"
        )

        # 5) 收窄网络：断开安装网络，接入 internal（只能经 Squid 到 capture）
        context.docker.switch_network(
            agent_container,
            context.network_info["install_network"],
            context.network_info["internal_network"],
        )
        _verify_network_locked(context, agent_container)

        # 6) 官方提示（单次执行，无 feedback；不调用轨迹摘要模型）
        context.check_stop()
        prompt = prompts.build_prompt(
            mode=context.mode,
            repo_dir="/src/" + _repo_to_patch(workspace_inputs),
            repo_to_patch=_repo_to_patch(workspace_inputs),
            prompt_style=cfg.prompt_style,
        )
        (attempt_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

        # 7) CC 推理：持续写 host console.log
        context.check_stop()
        run_started = time.monotonic()
        cc_result = cc_runner.run(
            agent_container,
            prompt,
            attempt_dir / "logs" / "console.log",
            env=cc_env,
            timeout_seconds=cfg.agent_timeout_seconds,
            stop_check=context.stop_check,
        )
        timings["agent_run"] = round(time.monotonic() - run_started, 3)
        agent_exit_code = cc_result.exit_code
        agent_exit_reason = cc_result.stop_reason or f"exit_{cc_result.exit_code}"
        if cc_result.stop_reason:
            limits.append(cc_result.stop_reason)

        # 8) 产物与会话导出（会话目录已通过挂载持久化在 host 侧）
        _copy_if_present(context.docker, agent_container, "/output/poc.bin",
                         attempt_dir / "output" / "poc.bin")
        _copy_if_present(context.docker, agent_container, "/output/fix.patch",
                         attempt_dir / "output" / "fix.patch")
        agent_generated["poc"] = (attempt_dir / "output" / "poc.bin").exists()
        agent_generated["patch"] = (attempt_dir / "output" / "fix.patch").exists()

        # 可选：保存完整工作区（默认只保存候选产物、日志和证据）
        if cfg.save_full_workspace:
            workspace_dir = attempt_dir / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            with _suppress():
                context.docker.copy_from(agent_container, "/src", workspace_dir / "src")

        # 9) 保存退出证据后精确结束 Agent 容器（先 Agent，再验证阶段）
        exit_evidence = context.docker.save_exit_evidence(
            agent_container, attempt_dir / "logs" / "agent_exit"
        )
        _ = exit_evidence
        context.docker.remove_container(
            agent_container, verify_labels={
                "cybergym_e2e.scope": context.scope,
                "cybergym_e2e.role": "agent",
                "cybergym_e2e.attempt_id": context.attempt_id,
            }
        )
    except AttemptAborted as aborted:
        _release(context, image, lease_released)
        return _interrupted_outcome(
            context, stage_records, agent_exit_code, agent_exit_reason,
            agent_generated, timings, limits, aborted.reason, infra_error,
        )
    except Exception as exc:  # noqa: BLE001 - infra error 归档，不无限重试
        infra_error = f"{type(exc).__name__}: {exc}"
        for stage in stage_policy.enabled_stages:
            stage_records.setdefault(stage, make_infra_error_record(
                stage, "agent_phase", stage_description(stage)))
        _release(context, image, lease_released)
        return _finalize_result(
            context, stage_records, agent_exit_code, agent_exit_reason,
            agent_generated, timings, limits, infra_error,
        )
    finally:
        # Agent 阶段意外异常时也确保容器清理（finally 中再次精确删除）
        with _suppress():
            if _container_exists(context.docker, agent_name):
                context.docker.save_exit_evidence(
                    agent_name, attempt_dir / "logs" / "agent_exit_unexpected"
                )
                context.docker.remove_container(
                    agent_name, verify_labels={"cybergym_e2e.scope": context.scope}
                )

    # 10) 每个启用阶段：全新容器、可信 prepare、独立验证
    lease_released = False
    try:
        for stage in stage_policy.enabled_stages:
            context.check_stop()
            record = _run_single_stage(
                context, image, stage,
                attempt_dir, workspace_inputs,
            )
            stage_records[stage] = record
            timings[f"stage{stage}"] = record.duration_seconds() or 0.0
    except AttemptAborted as aborted:
        for stage in stage_policy.enabled_stages:
            if stage not in stage_records:
                stage_records[stage] = make_interrupted_record(
                    stage, aborted.reason, stage_description(stage))
        limits.append(aborted.reason)
        _release(context, image, lease_released)
        return _interrupted_outcome(
            context, stage_records, agent_exit_code, agent_exit_reason,
            agent_generated, timings, limits, aborted.reason, infra_error,
        )
    except Exception as exc:  # noqa: BLE001
        infra_error = f"{type(exc).__name__}: {exc}"
        _release(context, image, lease_released)
        return _finalize_result(
            context, stage_records, agent_exit_code, agent_exit_reason,
            agent_generated, timings, limits, infra_error,
        )

    # 11) 禁用阶段记 disabled（不能填成失败或通过）
    for stage in set(range(1, 5)) - set(stage_policy.enabled_stages):
        stage_records[stage] = make_disabled_record(stage, stage_description(stage))

    _release(context, image, lease_released)
    return _finalize_result(
        context, stage_records, agent_exit_code, agent_exit_reason,
        agent_generated, timings, limits, infra_error,
    )


# ---------------------------------------------------------------------------
# 单阶段验证
# ---------------------------------------------------------------------------


def _run_single_stage(
    context: AttemptContext,
    image: str,
    stage: int,
    attempt_dir: Path,
    workspace_inputs: WorkspaceInputs,
) -> StageRecord:
    """全新容器执行一个启用阶段：独立于其他阶段的成败。"""
    cfg = context.cfg
    description = stage_description(stage)
    started = time.monotonic()
    stage_name = f"cg-e2e-s{stage}-{context.attempt_id}"[:63]

    # 输入需求检查：缺失只影响需要该输入的阶段
    poc_path = attempt_dir / "output" / "poc.bin"
    patch_path = attempt_dir / "output" / "fix.patch"
    gt_poc_path = workspace_inputs.data_dir / "poc.bin"
    available = {
        "poc": poc_path.exists(),
        "patch": patch_path.exists(),
        "gt_poc": gt_poc_path.exists(),
    }
    required = STAGE_INPUT_REQUIREMENTS[stage]
    missing = sorted(name for name in required if not available[name])
    input_hashes = {
        name: util.sha256_file(path)
        for name, path, present in (
            ("poc", poc_path, available["poc"]),
            ("patch", patch_path, available["patch"]),
            ("gt_poc", gt_poc_path, available["gt_poc"]),
        )
        if present and name in required
    }
    if missing:
        return make_missing_input_record(stage, set(missing), description)

    stage_dir = attempt_dir / f"stage{stage}"
    stage_dir.mkdir(parents=True, exist_ok=True)

    spec = ContainerSpec(
        image=image,
        name=stage_name,
        role=f"stage{stage}",
        scope=context.scope,
        attempt_id=context.attempt_id,
        launch_id=context.launch_id,
        network=context.network_info["install_network"],
        workdir="/src",
        memory=cfg.validation_memory,
        cpus=cfg.validation_cpus,
        pids_limit=cfg.validation_pids_limit,
        core_limit_bytes=0,
    )
    setup = WorkspaceSetup(
        context.docker,
        WorkspaceInputs(
            task_path=workspace_inputs.task_path,
            mode=workspace_inputs.mode,
            frozen_dir=workspace_inputs.frozen_dir,
            repo_dir=workspace_inputs.repo_dir,
            build_image_source=workspace_inputs.build_image_source,
            copy_gt_poc="gt_poc" in required,  # S4 需要参考输入
        ),
    )

    container_id = None
    try:
        container_info = context.docker.create_and_start(spec)
        container_id = container_info["Id"]

        # 可信 prepare 完成后收窄网络：验证阶段断开全部网络（仅 loopback）
        setup_started = time.monotonic()
        setup.setup(container_id, run_prepare=True)
        if time.monotonic() - setup_started > cfg.stage_setup_timeout_seconds:
            raise AttemptAborted(f"stage{stage}_setup_budget_exceeded")
        context.docker.switch_network(container_id, context.network_info["install_network"], None)

        # 候选文件（host 冻结副本，不挂载 Agent 原始工作目录）
        if available["poc"] and "poc" in required:
            context.docker.copy_to(container_id, poc_path, "/output/poc.bin")
        if available["patch"]:
            context.docker.copy_to(container_id, patch_path, "/output/fix.patch")

        command = (
            "/scripts/.venv/bin/python /scripts/validation_entry.py "
            f"--only-stage {stage} --json-output /output/stage{stage}.json "
            "--src-dir /src --data-dir /data --config-dir /config"
        )
        if available["poc"] and "poc" in required:
            command += " --poc-file /output/poc.bin"
        if available["patch"]:
            command += " --patch-file /output/fix.patch"

        # 注入薄入口（保存官方裁判完整返回值）
        entry_source = Path(__file__).with_name("validation_entry.py")
        context.docker.copy_to(container_id, entry_source, "/scripts/validation_entry.py")

        exit_code, stop_reason = context.docker.exec_stream_to_file(
            container_id,
            command,
            stage_dir / f"stage{stage}.log",
            timeout=cfg.stage_validation_timeout_seconds,
            workdir="/",
            poll_stop=context.stop_check,
        )
        if stop_reason:
            raise AttemptAborted(stop_reason)

        result_json_path = stage_dir / f"stage{stage}.json"
        copied = context.docker.copy_from(
            container_id, f"/output/stage{stage}.json", result_json_path
        )
        if not copied or not result_json_path.exists():
            raise DockerControlError(f"stage{stage} 结果 JSON 未生成 (exit={exit_code})")
        payload = util.read_json(result_json_path)
        stage_result = (payload.get("results") or {}).get(f"stage{stage}") or {}
        raw_status = stage_result.get("status")
        normalized, reason = normalize_official_status(raw_status)

        record = StageRecord(
            stage=stage,
            raw_status=raw_status,
            normalized_status=normalized,
            reason=reason,
            started_at=started,
            finished_at=time.monotonic(),
            exit_code=exit_code,
            input_hashes=input_hashes,
            evidence_paths={
                "log": str(stage_dir / f"stage{stage}.log"),
                "result_json": str(result_json_path),
            },
            output_excerpt=str(stage_result.get("output") or "")[:4000] or None,
            description=description,
        )
        return record
    except AttemptAborted:
        return make_interrupted_record(stage, "stop_during_stage", description)
    except DockerControlError as exc:
        # Docker 不可用/容器异常：无法获得判定证据 → 外壳 error（标明来源）
        return make_infra_error_record(stage, f"docker:{exc}", description)
    except Exception as exc:  # noqa: BLE001
        return make_infra_error_record(stage, f"{type(exc).__name__}: {exc}", description)
    finally:
        if container_id or _container_exists(context.docker, stage_name):
            with _suppress():
                context.docker.save_exit_evidence(stage_name, stage_dir / "container_exit")
                context.docker.remove_container(
                    stage_name,
                    verify_labels={"cybergym_e2e.scope": context.scope},
                )


# ---------------------------------------------------------------------------
# 结果组装
# ---------------------------------------------------------------------------


def _finalize_result(
    context: AttemptContext,
    stage_records: dict[int, StageRecord],
    agent_exit_code: int | None,
    agent_exit_reason: str | None,
    agent_generated: dict[str, bool],
    timings: dict[str, float],
    limits: list[str],
    infra_error: str | None,
) -> AttemptOutcome:
    result = AttemptResult(
        task_path=context.task_path,
        mode=context.mode,
        repeat_index=context.repeat_index,
        generation=context.generation,
        attempt_id=context.attempt_id,
        scope=context.scope,
        launch_id=context.launch_id,
        model=context.cfg.model,
        claude_code_version=context.cfg.claude_code_version,
        stage_records=stage_records,
        success_stages=context.cfg.stage_policy.success_stages,
        enabled_stages=context.cfg.stage_policy.enabled_stages,
        agent_exit_code=agent_exit_code,
        agent_exit_reason=agent_exit_reason,
        agent_generated=agent_generated,
        timings=timings,
        limits_triggered=limits,
        artifact_hashes=_artifact_hashes(context.attempt_dir),
        infra_error=infra_error,
    )
    receipt = write_result_and_receipt(
        context.attempt_dir, result, context.cfg.stage_policy.enabled_stages
    )
    payload = util.read_json(Path(context.attempt_dir) / "result.json")
    outcome, error = classify_attempt_outcome(payload)
    _ = receipt
    return AttemptOutcome(
        outcome=outcome, error=error, attempt_dir=Path(context.attempt_dir), result_json=payload
    )


def _interrupted_outcome(
    context: AttemptContext,
    stage_records: dict[int, StageRecord],
    agent_exit_code: int | None,
    agent_exit_reason: str | None,
    agent_generated: dict[str, bool],
    timings: dict[str, float],
    limits: list[str],
    reason: str,
    infra_error: str | None,
) -> AttemptOutcome:
    for stage in context.cfg.stage_policy.enabled_stages:
        stage_records.setdefault(stage, make_interrupted_record(
            stage, reason, stage_description(stage)))
    for stage in set(range(1, 5)) - set(context.cfg.stage_policy.enabled_stages):
        stage_records.setdefault(stage, make_disabled_record(stage, stage_description(stage)))
    return _finalize_result(
        context, stage_records, agent_exit_code, agent_exit_reason,
        agent_generated, timings, limits, infra_error,
    )


def _artifact_hashes(attempt_dir: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name in ("poc.bin", "fix.patch"):
        path = Path(attempt_dir) / "output" / name
        if path.exists():
            hashes[name] = util.sha256_file(path)
    console = Path(attempt_dir) / "logs" / "console.log"
    if console.exists():
        hashes["console.log"] = util.sha256_file(console)
    return hashes


def _release(context: AttemptContext, image: str, lease_released: bool) -> None:
    if lease_released:
        return
    with _suppress():
        from .image_manager import ImageLease

        context.images.release_lease(
            ImageLease(
                image=image, scope=context.scope, launch_id=context.launch_id,
                attempt_id=context.attempt_id, pid=os.getpid(), pid_start="",
            )
        )


def _repo_to_patch(inputs: WorkspaceInputs) -> str:
    return str(inputs.merged_config().get("repo_to_patch", ""))


def _verify_network_locked(context: AttemptContext, container: str) -> None:
    """断网核验：agent 只能经 Squid 访问 capture；直连 host 应失败。"""
    _ = context
    _ = container
    # 反向核验由 network_reverse_test.sh 在启动时统一执行；
    # 这里不再对每个 attempt 重复探测，避免额外容器创建。


def _chown_recursive(path: Path, uid: int, gid: int) -> None:
    import stat as _stat

    path = Path(path)
    os.chown(path, uid, gid)
    for child in path.rglob("*"):
        try:
            os.chown(child, uid, gid)
            if child.is_dir() and (_stat.S_ISLNK(child.lstat().st_mode) is False):
                os.chmod(child, 0o755)
        except OSError:
            continue


def _copy_if_present(docker: DockerControl, container: str, source: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    docker.copy_from(container, source, destination)


def _container_exists(docker: DockerControl, name: str) -> bool:
    return docker.inspect(name) is not None


def _json_dump(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2)


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return True
