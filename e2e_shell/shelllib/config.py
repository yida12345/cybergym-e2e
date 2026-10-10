"""配置解析、合法性检查与派生路径。

可修改参数集中在 00_config.sh 顶部（中文注释）；本模块读取环境变量并
解析为强类型配置对象。所有进入 manifest 的值都来自这里，凭据除外：
本模块拒绝把 API key 之类的秘密放进任何序列化输出。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import util

ALL_STAGES = (1, 2, 3, 4)
MODES = ("e2e", "patch-only")

# patch-only 模式下 Agent 只生成补丁，没有要求它生成自己的 PoC，
# 因此 S1/S2（依赖 Agent PoC）在该模式下没有意义。
MODE_FORBIDDEN_STAGES = {"patch-only": {1, 2}}

# 两个模式的默认阶段集合与成功集合。
DEFAULT_STAGES: dict[str, tuple[list[int], list[int]]] = {
    "e2e": ([1, 2, 3, 4], [1, 2, 3]),
    "patch-only": ([3, 4], [3, 4]),
}


class ConfigError(ValueError):
    """配置不合法；run_all 直接报错退出，不启动任何资源。"""


def _parse_stage_list(raw: str, field_name: str) -> list[int]:
    """解析 'auto'、空格或逗号分隔的阶段编号；去重并按编号排序。"""
    text = (raw or "").strip()
    if not text:
        raise ConfigError(f"{field_name} 不能为空；使用 auto 或 1-4 的编号列表")
    if text.lower() == "auto":
        return []  # 由 MODE 决定，调用方展开
    stages: list[int] = []
    seen: set[int] = set()
    for token in re.split(r"[,\s]+", text):
        token = token.strip()
        if not token:
            continue
        if not re.fullmatch(r"[1-4]", token):
            raise ConfigError(f"{field_name} 含非法阶段编号 {token!r}；只支持 1-4")
        value = int(token)
        if value not in seen:
            stages.append(value)
            seen.add(value)
    if not stages:
        raise ConfigError(f"{field_name} 解析结果为空")
    return sorted(stages)


def resolve_stages(mode: str, enabled_raw: str, success_raw: str) -> tuple[list[int], list[int]]:
    """解析 ENABLED_STAGES / SUCCESS_STAGES 并校验全部约束。

    - auto 按模式展开默认集合；
    - 成功条件必须是启用阶段的非空子集；
    - patch-only 不接受 S1/S2；
    - 阶段编号只能是 1-4，去重、规范排序。
    """
    if mode not in MODES:
        raise ConfigError(f"MODE 必须是 {' 或 '.join(MODES)}，当前为 {mode!r}")
    forbidden = MODE_FORBIDDEN_STAGES.get(mode, set())

    if enabled_raw.strip().lower() == "auto":
        enabled = list(DEFAULT_STAGES[mode][0])
    else:
        enabled = _parse_stage_list(enabled_raw, "ENABLED_STAGES")
    if success_raw.strip().lower() == "auto":
        success = list(DEFAULT_STAGES[mode][1])
    else:
        success = _parse_stage_list(success_raw, "SUCCESS_STAGES")

    for name, stages in (("ENABLED_STAGES", enabled), ("SUCCESS_STAGES", success)):
        bad = sorted(forbidden.intersection(stages))
        if bad:
            raise ConfigError(
                f"MODE={mode} 不支持 {name} 中的阶段 {bad}；该模式只允许 "
                f"{sorted(set(ALL_STAGES) - forbidden)}"
            )

    if not enabled:
        raise ConfigError("ENABLED_STAGES 解析结果不能为空")
    if not success:
        raise ConfigError("SUCCESS_STAGES 解析结果不能为空")
    if not set(success).issubset(set(enabled)):
        raise ConfigError(
            f"SUCCESS_STAGES {success} 必须是 ENABLED_STAGES {enabled} 的子集"
        )
    return enabled, success


@dataclass
class StagePolicy:
    """阶段执行与成功分组策略。"""

    mode: str
    enabled_stages: list[int]
    success_stages: list[int]

    @property
    def is_custom_profile(self) -> bool:
        default_enabled, default_success = DEFAULT_STAGES[self.mode]
        return (
            self.enabled_stages != default_enabled or self.success_stages != default_success
        )

    def policy_hash(self) -> str:
        return util.scope_hash(
            "stage-policy",
            self.mode,
            ",".join(map(str, self.enabled_stages)),
            ",".join(map(str, self.success_stages)),
        )

    def to_manifest(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "enabled_stages": list(self.enabled_stages),
            "success_stages": list(self.success_stages),
            "default_profile": not self.is_custom_profile,
            "policy_hash": self.policy_hash(),
        }


@dataclass
class ShellConfig:
    """外壳全部运行参数；来源是环境变量（00_config.sh export）。"""

    # 实验与输入
    exp: str
    round_number: int
    output_root: Path
    repo_dir: Path
    dataset_dir: Path
    image_mapping_file: Path
    source_task_file: Path
    exclude_completed_task_file: Path | None
    include_projects: list[str]
    include_tasks: list[str]
    shuffle_tasks: bool
    shuffle_seed: int
    data_freeze_mode: str

    # 模式与阶段
    stage_policy: StagePolicy

    # 模型与 CC
    model: str
    reasoning_effort: str
    claude_code_version: str
    model_provider: str
    agent: str
    prompt_style: str
    cc_runtime_dir: Path
    cc_install_mode: str
    model_api_key_env: str

    # 并发与重复
    workers: int
    task_repeat_n: int
    worker_max_restarts: int
    resume_policy: str
    infra_error_policy: str

    # 容器资源
    agent_cpus: float
    validation_cpus: float
    agent_memory: str
    validation_memory: str
    agent_pids_limit: int
    validation_pids_limit: int
    agent_core_limit_bytes: int

    # 时间预算（秒）
    agent_timeout_seconds: int
    agent_setup_timeout_seconds: int
    stage_setup_timeout_seconds: int
    stage_validation_timeout_seconds: int
    cleanup_budget_seconds: int
    stop_grace_seconds: int
    agent_api_timeout_ms: int
    agent_api_max_retries: int
    agent_result_idle_timeout_seconds: int
    agent_api_force_idle_timeout: int
    agent_max_output_tokens: int

    # Docker
    docker_api_timeout_seconds: int
    docker_timeout_retries: int
    docker_retry_delay_seconds: int
    docker_startup_concurrency: int
    docker_startup_lock_dir: Path

    # 资源共存预算
    host_reserved_cpus: float
    host_reserved_memory_gb: float
    min_host_free_gb: float
    disk_check_interval_seconds: int
    max_attempt_output_gb: float
    max_container_rw_gb: float

    # capture / proxy
    capture_proxy_dir: Path
    capture_proxy_python: Path
    capture_proxy_port: int
    capture_route_prefix: str
    capture_upstream_url: str
    capture_bind_host: str
    capture_timeout_seconds: float
    capture_connect_timeout_seconds: float
    capture_read_timeout_seconds: float
    capture_attempt_timeout_seconds: float
    capture_request_timeout_seconds: float
    capture_max_retries: int
    capture_retry_delay_seconds: float
    capture_retry_max_delay_seconds: float
    capture_drain_timeout_seconds: float
    capture_smoke_timeout_seconds: float
    capture_proxy_entrypoint: Path

    # 输出与导出
    export_sharegpt: bool
    auto_finalize_on_complete: bool
    online_export_enabled: bool
    online_workers: int
    online_interval_seconds: int
    status_poll_seconds: float
    save_full_workspace: bool
    image_cleanup_enabled: bool

    # 派生路径与 scope
    run_scope: Path = field(init=False)
    state_dir: Path = field(init=False)
    log_dir: Path = field(init=False)
    attempt_root: Path = field(init=False)
    frozen_dir: Path = field(init=False)
    dataset_projects_dir: Path = field(init=False)

    @property
    def scope(self) -> str:
        """规范化 scope 标识；用于容器、网络、锁和 label。"""
        return util.scope_hash(
            "cg-e2e", str(self.output_root.resolve()), self.exp, str(self.round_number)
        )

    def __post_init__(self) -> None:
        self.output_root = Path(self.output_root).resolve()
        self.repo_dir = Path(self.repo_dir).resolve()
        self.dataset_dir = Path(self.dataset_dir).resolve()
        self.image_mapping_file = Path(self.image_mapping_file).resolve()
        self.source_task_file = Path(self.source_task_file).resolve()
        self.run_scope = self.output_root / "runs" / self.exp / f"round_{self.round_number}"
        self.state_dir = self.run_scope / "state"
        self.log_dir = self.run_scope / "logs"
        self.attempt_root = self.run_scope / "attempts"
        self.frozen_dir = self.run_scope / "frozen"
        self.dataset_projects_dir = self.dataset_dir / "projects"

    def derive(self) -> None:
        """创建派生目录；幂等。"""
        for path in (
            self.output_root,
            self.run_scope,
            self.state_dir,
            self.log_dir,
            self.attempt_root,
            self.run_scope / "capture_logs",
            self.output_root / "shared_state",
            self.output_root / "tools",
        ):
            path.mkdir(parents=True, exist_ok=True)

    def queue_db_path(self) -> Path:
        return self.state_dir / "queue.sqlite3"

    def manifest_path(self) -> Path:
        return self.run_scope / "manifest.json"

    def launch_dir(self) -> Path:
        return self.state_dir / "launches"

    def core_fingerprint(self) -> dict[str, Any]:
        """核心参数指纹：改变这些需要新实验，而不是续跑。"""
        return {
            "exp": self.exp,
            "round_number": self.round_number,
            "stage_policy": self.stage_policy.to_manifest(),
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "claude_code_version": self.claude_code_version,
            "model_provider": self.model_provider,
            "agent": self.agent,
            "prompt_style": self.prompt_style,
            "task_repeat_n": self.task_repeat_n,
            "agent_timeout_seconds": self.agent_timeout_seconds,
            "agent_max_output_tokens": self.agent_max_output_tokens,
            "agent_memory": self.agent_memory,
            "validation_memory": self.validation_memory,
            "agent_cpus": self.agent_cpus,
            "validation_cpus": self.validation_cpus,
            "repo_commit": repo_head_commit(self.repo_dir),
            "repo_script_fingerprints": repo_script_fingerprints(self.repo_dir),
            "image_mapping_hash": (
                util.sha256_file(self.image_mapping_file)
                if self.image_mapping_file.exists()
                else None
            ),
            "source_task_file_hash": (
                util.sha256_file(self.source_task_file) if self.source_task_file.exists() else None
            ),
        }

    def runtime_fingerprint(self) -> dict[str, Any]:
        """运行参数指纹：续跑允许调整的参数记录进新 launch。"""
        return {
            "workers": self.workers,
            "status_poll_seconds": self.status_poll_seconds,
            "online_workers": self.online_workers,
            "online_interval_seconds": self.online_interval_seconds,
            "stop_grace_seconds": self.stop_grace_seconds,
        }


def repo_head_commit(repo_dir: Path) -> str | None:
    """官方仓库当前 HEAD；输入冻结记录在 manifest。"""
    head = Path(repo_dir) / ".git" / "HEAD"
    if not head.exists():
        return None
    try:
        text = head.read_text().strip()
    except OSError:
        return None
    if text.startswith("ref: "):
        ref_path = Path(repo_dir) / ".git" / text[5:]
        try:
            return ref_path.read_text().strip()
        except OSError:
            return None
    return text


REPO_KEY_SCRIPTS = (
    "scripts/run_agent.py",
    "scripts/utils.py",
    "scripts/validate.py",
    "scripts/firewall/proxy.py",
)


def repo_script_fingerprints(repo_dir: Path) -> dict[str, str]:
    """官方关键脚本 hash：启动前核对接口版本，防止仓库更新后静默误用。"""
    fingerprints: dict[str, str] = {}
    for relative in REPO_KEY_SCRIPTS:
        path = Path(repo_dir) / relative
        if path.exists():
            fingerprints[relative] = util.sha256_file(path)
    return fingerprints


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数，当前为 {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是数字，当前为 {raw!r}") from exc


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "") or ""
    return [item.strip() for item in re.split(r"[,\s]+", raw) if item.strip()]


def load_config_from_env() -> ShellConfig:
    """从环境变量（00_config.sh export）构建并校验配置。"""
    exp = os.environ.get("EXP", "").strip()
    if not exp:
        raise ConfigError("EXP 不能为空")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", exp):
        raise ConfigError(f"EXP 只能包含字母、数字、下划线、点和连字符: {exp!r}")
    if ".." in exp or exp.startswith("."):
        raise ConfigError(f"EXP 不允许目录穿越形式: {exp!r}")

    round_number = _env_int("ROUND_NUMBER", 1)
    if round_number < 1:
        raise ConfigError("ROUND_NUMBER 必须 >= 1")

    mode = os.environ.get("MODE", "e2e").strip()
    enabled, success = resolve_stages(
        mode,
        os.environ.get("ENABLED_STAGES", "auto"),
        os.environ.get("SUCCESS_STAGES", "auto"),
    )

    workers = _env_int("WORKERS", 8)
    if workers < 1:
        raise ConfigError("WORKERS 必须 >= 1")
    repeat = _env_int("TASK_REPEAT_N", 2)
    if repeat < 1:
        raise ConfigError("TASK_REPEAT_N 必须 >= 1")

    freeze_mode = os.environ.get("DATA_FREEZE_MODE", "hardlink").strip()
    if freeze_mode not in ("hardlink", "copy"):
        raise ConfigError("DATA_FREEZE_MODE 只能是 hardlink 或 copy")

    agent = os.environ.get("AGENT", "claude-code")
    if agent != "claude-code":
        raise ConfigError(f"当前规格只实现 claude-code 后端，AGENT={agent!r}")
    prompt_style = os.environ.get("PROMPT_STYLE", "iterative")
    if prompt_style not in ("iterative", "no-test"):
        raise ConfigError(f"PROMPT_STYLE 非法: {prompt_style!r}")

    effort = os.environ.get("REASONING_EFFORT", "high").strip().lower()
    if effort not in ("low", "medium", "high", "xhigh", "max", "auto"):
        raise ConfigError(f"REASONING_EFFORT 非法: {effort!r}")

    cc_install_mode = os.environ.get("CC_INSTALL_MODE", "shared-runtime")
    if cc_install_mode not in ("shared-runtime", "per-container"):
        raise ConfigError("CC_INSTALL_MODE 只能是 shared-runtime 或 per-container")

    if os.environ.get("MODEL_PROVIDER", "anthropic") != "anthropic":
        raise ConfigError("当前规格只支持 MODEL_PROVIDER=anthropic")

    memory_re = re.fullmatch(r"(\d+)([gGmM]?)", os.environ.get("AGENT_MEMORY", "12g") or "")
    if not memory_re:
        raise ConfigError("AGENT_MEMORY 必须形如 12g / 512m")
    if not re.fullmatch(r"(\d+)([gGmM]?)", os.environ.get("VALIDATION_MEMORY", "16g") or ""):
        raise ConfigError("VALIDATION_MEMORY 必须形如 16g / 512m")

    capture_port = _env_int("CAPTURE_PROXY_PORT", 30305)
    if not (1 <= capture_port <= 65535):
        raise ConfigError("CAPTURE_PROXY_PORT 必须在 1-65535")

    startup_concurrency = _env_int("DOCKER_STARTUP_CONCURRENCY", 8)
    if startup_concurrency < 1:
        raise ConfigError("DOCKER_STARTUP_CONCURRENCY 必须 >= 1")

    exclude_raw = os.environ.get("EXCLUDE_COMPLETED_TASK_FILE", "").strip()
    cfg = ShellConfig(
        exp=exp,
        round_number=round_number,
        output_root=_env_path("OUTPUT_ROOT", "/data/cybergym-e2e/output"),
        repo_dir=_env_path("REPO_DIR", "/data/cybergym-e2e/cybergym-e2e"),
        dataset_dir=_env_path("DATASET_DIR", "/data/cybergym-e2e/output/dataset"),
        image_mapping_file=_env_path(
            "IMAGE_MAPPING_FILE",
            "/data/cybergym-e2e/acr_image_mirror/runs/default/image_mapping.json",
        ),
        source_task_file=_env_path(
            "SOURCE_TASK_FILE", "/data/cybergym-e2e/cybergym-e2e/scripts/tasks.txt"
        ),
        exclude_completed_task_file=Path(exclude_raw) if exclude_raw else None,
        include_projects=_env_list("INCLUDE_PROJECTS"),
        include_tasks=_env_list("INCLUDE_TASKS"),
        shuffle_tasks=_env_bool("SHUFFLE_TASKS", True),
        shuffle_seed=_env_int("SHUFFLE_SEED", 42),
        data_freeze_mode=freeze_mode,
        stage_policy=StagePolicy(mode=mode, enabled_stages=enabled, success_stages=success),
        model=os.environ.get("MODEL", "glm53"),
        reasoning_effort=effort,
        claude_code_version=os.environ.get("CLAUDE_CODE_VERSION", "2.1.91"),
        model_provider="anthropic",
        agent=agent,
        prompt_style=prompt_style,
        cc_runtime_dir=_env_path("CC_RUNTIME_DIR", "/data/cybergym-e2e/output/tools/cc-runtime"),
        cc_install_mode=cc_install_mode,
        model_api_key_env=os.environ.get("MODEL_API_KEY_ENV", "ANTHROPIC_API_KEY"),
        workers=workers,
        task_repeat_n=repeat,
        worker_max_restarts=_env_int("WORKER_MAX_RESTARTS", 3),
        resume_policy=os.environ.get("RESUME_POLICY", "auto"),
        infra_error_policy=os.environ.get("INFRA_ERROR_POLICY", "record"),
        agent_cpus=_env_float("AGENT_CPUS", 2),
        validation_cpus=_env_float("VALIDATION_CPUS", 2),
        agent_memory=os.environ.get("AGENT_MEMORY", "12g"),
        validation_memory=os.environ.get("VALIDATION_MEMORY", "16g"),
        agent_pids_limit=_env_int("AGENT_PIDS_LIMIT", 2048),
        validation_pids_limit=_env_int("VALIDATION_PIDS_LIMIT", 2048),
        agent_core_limit_bytes=_env_int("AGENT_CORE_LIMIT_BYTES", 0),
        agent_timeout_seconds=_env_int("AGENT_TIMEOUT_SECONDS", 604800),
        agent_setup_timeout_seconds=_env_int("AGENT_SETUP_TIMEOUT_SECONDS", 10800),
        stage_setup_timeout_seconds=_env_int("STAGE_SETUP_TIMEOUT_SECONDS", 7200),
        stage_validation_timeout_seconds=_env_int("STAGE_VALIDATION_TIMEOUT_SECONDS", 7200),
        cleanup_budget_seconds=_env_int("CLEANUP_BUDGET_SECONDS", 1800),
        stop_grace_seconds=_env_int("STOP_GRACE_SECONDS", 60),
        agent_api_timeout_ms=_env_int("AGENT_API_TIMEOUT_MS", 90000000),
        agent_api_max_retries=_env_int("AGENT_API_MAX_RETRIES", 3),
        agent_result_idle_timeout_seconds=_env_int("AGENT_RESULT_IDLE_TIMEOUT_SECONDS", 300),
        agent_api_force_idle_timeout=_env_int("AGENT_API_FORCE_IDLE_TIMEOUT", 0),
        agent_max_output_tokens=_env_int("AGENT_MAX_OUTPUT_TOKENS", 64000),
        docker_api_timeout_seconds=_env_int("DOCKER_API_TIMEOUT_SECONDS", 600),
        docker_timeout_retries=_env_int("DOCKER_TIMEOUT_RETRIES", 3),
        docker_retry_delay_seconds=_env_int("DOCKER_RETRY_DELAY_SECONDS", 10),
        docker_startup_concurrency=startup_concurrency,
        docker_startup_lock_dir=_env_path(
            "DOCKER_STARTUP_LOCK_DIR", "/tmp/cybergym-docker-startup-slots"
        ),
        host_reserved_cpus=_env_float("HOST_RESERVED_CPUS", 8),
        host_reserved_memory_gb=_env_float("HOST_RESERVED_MEMORY_GB", 32),
        min_host_free_gb=_env_float("MIN_HOST_FREE_GB", 100),
        disk_check_interval_seconds=_env_int("DISK_CHECK_INTERVAL_SECONDS", 5),
        max_attempt_output_gb=_env_float("MAX_ATTEMPT_OUTPUT_GB", 20),
        max_container_rw_gb=_env_float("MAX_CONTAINER_RW_GB", 16),
        capture_proxy_dir=_env_path(
            "CAPTURE_PROXY_DIR", "/data/ztr/anthropic_full_capture_proxy"
        ),
        capture_proxy_python=_env_path("CAPTURE_PROXY_PYTHON", "/usr/bin/env python3"),
        capture_proxy_port=capture_port,
        capture_route_prefix=os.environ.get("CAPTURE_ROUTE_PREFIX", "/glm53"),
        capture_upstream_url=os.environ.get(
            "CAPTURE_UPSTREAM_URL", "http://8.130.156.14:8000"
        ).rstrip("/"),
        capture_bind_host=os.environ.get("CAPTURE_BIND_HOST", ""),
        capture_timeout_seconds=_env_float("CAPTURE_TIMEOUT_SECONDS", 300),
        capture_connect_timeout_seconds=_env_float("CAPTURE_CONNECT_TIMEOUT_SECONDS", 30),
        capture_read_timeout_seconds=_env_float("CAPTURE_READ_TIMEOUT_SECONDS", 600),
        capture_attempt_timeout_seconds=_env_float("CAPTURE_ATTEMPT_TIMEOUT_SECONDS", 3600),
        capture_request_timeout_seconds=_env_float("CAPTURE_REQUEST_TIMEOUT_SECONDS", 86400),
        capture_max_retries=_env_int("CAPTURE_MAX_RETRIES", 20),
        capture_retry_delay_seconds=_env_float("CAPTURE_RETRY_DELAY_SECONDS", 1),
        capture_retry_max_delay_seconds=_env_float("CAPTURE_RETRY_MAX_DELAY_SECONDS", 30),
        capture_drain_timeout_seconds=_env_float("CAPTURE_DRAIN_TIMEOUT_SECONDS", 600),
        capture_smoke_timeout_seconds=_env_float("CAPTURE_SMOKE_TIMEOUT_SECONDS", 86700),
        capture_proxy_entrypoint=_env_path(
            "CAPTURE_PROXY_ENTRYPOINT",
            "/data/ztr/anthropic_full_capture_proxy/proxy_retry.py",
        ),
        export_sharegpt=_env_bool("EXPORT_SHAREGPT", True),
        auto_finalize_on_complete=_env_bool("AUTO_FINALIZE_ON_COMPLETE", True),
        online_export_enabled=_env_bool("ONLINE_EXPORT_ENABLED", False),
        online_workers=_env_int("ONLINE_WORKERS", 2),
        online_interval_seconds=_env_int("ONLINE_INTERVAL_SECONDS", 300),
        status_poll_seconds=_env_float("STATUS_POLL_SECONDS", 5),
        save_full_workspace=_env_bool("SAVE_FULL_WORKSPACE", False),
        image_cleanup_enabled=_env_bool("IMAGE_CLEANUP_ENABLED", True),
    )
    _validate_paths(cfg)
    return cfg


def _validate_paths(cfg: ShellConfig) -> None:
    """路径检查：全部绝对化，拒绝目录穿越，核对官方仓库结构。"""
    for name, path in (
        ("OUTPUT_ROOT", cfg.output_root),
        ("REPO_DIR", cfg.repo_dir),
        ("DATASET_DIR", cfg.dataset_dir),
        ("IMAGE_MAPPING_FILE", cfg.image_mapping_file),
        ("SOURCE_TASK_FILE", cfg.source_task_file),
    ):
        if not path.is_absolute():
            raise ConfigError(f"{name} 必须是绝对路径: {path}")
        if ".." in path.parts:
            raise ConfigError(f"{name} 不允许包含 ..: {path}")

    scripts_dir = cfg.repo_dir / "scripts"
    for required in (scripts_dir / "run_agent.py", scripts_dir / "validate.py", scripts_dir / "utils.py"):
        if not required.exists():
            raise ConfigError(f"官方仓库缺少必需文件: {required}")
    projects_dir = cfg.repo_dir / "projects"
    if not projects_dir.is_dir():
        raise ConfigError(f"官方仓库缺少 projects 目录: {projects_dir}")

    # run_scope 必须位于 OUTPUT_ROOT 下，防止 scope 逃逸。
    try:
        cfg.run_scope.relative_to(cfg.output_root)
    except ValueError as exc:
        raise ConfigError("实验输出必须位于 OUTPUT_ROOT 之内") from exc


def total_attempt_budget_seconds(cfg: ShellConfig) -> int:
    """总 attempt 预算 = Agent + Agent setup + Σ(阶段 setup+验证) + 清理余量。

    启动槽等待计入 setup 预算，不挤占 Agent 推理预算；该值与官方
    内部单条命令限额（prepare 1800s、compile 3600s 等）分开记录。
    """
    stage_budget = len(cfg.stage_policy.enabled_stages) * (
        cfg.stage_setup_timeout_seconds + cfg.stage_validation_timeout_seconds
    )
    return (
        cfg.agent_timeout_seconds
        + cfg.agent_setup_timeout_seconds
        + stage_budget
        + cfg.cleanup_budget_seconds
    )


def scrub_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    """递归移除疑似秘密的字段；manifest、日志与导出统一使用。

    按字段名分段（snake/kebab 拆词后整词匹配），避免把
    ``agent_max_output_tokens`` 这类含 tokens 的正常参数误判为凭据。
    """
    secret_segments = {
        "key", "apikey", "token", "secret", "password", "passwd",
        "credential", "credentials", "authorization", "auth",
    }

    def _is_secret_key(name: str) -> bool:
        segments = re.split(r"[_\-\s]+", str(name).lower())
        return any(segment in secret_segments for segment in segments)

    def _scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: ("<redacted>" if _is_secret_key(key) else _scrub(item))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [_scrub(item) for item in value]
        return value

    return _scrub(payload)
