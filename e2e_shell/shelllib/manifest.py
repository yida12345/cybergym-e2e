"""输入冻结：任务清单、项目/任务脚本、数据资产、代码指纹与 ACR 镜像替换。

02_prepare_dataset 调用本模块。冻结后的内容是后续所有 attempt 的唯一输入：
同 EXP 恢复复用冻结输入，不重新取远程 main、不重新随机排序、不覆盖旧配置。
核心指纹变化时拒绝混入旧实验。

冻结副本中只修改 host 侧的 build_image 字段（改为 ACR digest 引用）；
项目默认、任务覆盖与全局 fallback 的合并规则与官方 run_agent.main() 一致。
"""

from __future__ import annotations

import random
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import util
from .config import ShellConfig, scrub_secrets

TASK_SCRIPT_FILES = ("prepare.sh", "compile.sh", "run_poc.sh", "test.sh")
TASK_DATA_FILES = ("src.tgz", "poc.bin", "crash.log")
DEFAULT_BUILD_IMAGE = (
    "gcr.io/oss-fuzz-base/base-builder@sha256:"
    "8eda74a11e800aead5a041ee479a65b33dab3150d6e89e5694e2b6eb27be98fc"
)


class FreezeError(RuntimeError):
    pass


@dataclass
class TaskEntry:
    """一个最终任务的冻结信息。"""

    task_path: str  # 例如 curl/arvo_66012
    project: str
    task_name: str
    build_image_source: str  # 官方原始引用（合并 project/task/fallback 后）
    build_image_acr: str  # ACR digest 引用
    data_files: dict[str, str] = field(default_factory=dict)  # 文件名 -> sha256


def load_official_task_list(source_task_file: Path) -> list[str]:
    """读取官方任务清单；去重并保持可复核顺序。"""
    tasks: list[str] = []
    seen: set[str] = set()
    for line in source_task_file.read_text(encoding="utf-8").splitlines():
        task = line.strip()
        if not task or task.startswith("#"):
            continue
        if "/" not in task:
            raise FreezeError(f"任务路径缺少项目前缀: {task!r}")
        if task in seen:
            continue
        seen.add(task)
        tasks.append(task)
    return tasks


def load_completed_exclusions(path: Path | None) -> set[str]:
    if path is None:
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def load_image_mapping(mapping_file: Path) -> dict[str, Any]:
    """加载已有 ACR digest 映射；不做全量迁移。"""
    payload = util.read_json(mapping_file)
    if not isinstance(payload, dict) or "source_to_acr" not in payload:
        raise FreezeError(f"镜像映射文件结构不合法: {mapping_file}")
    return payload


def merge_build_image(repo_dir: Path, task_path: str) -> tuple[str, dict[str, Any]]:
    """按官方顺序合并 project.toml + task config.toml，得到有效 build_image。

    与官方 run_agent.main() 的 `config.update(task_toml)` 语义一致：
    任务覆盖 > 项目默认 > 全局 fallback。
    """
    project, task_name = task_path.split("/", 1)
    project_toml = repo_dir / "projects" / project / "project.toml"
    task_toml = repo_dir / "projects" / project / task_name / "config.toml"
    if not project_toml.exists():
        raise FreezeError(f"缺少项目配置: {project_toml}")
    if not task_toml.exists():
        raise FreezeError(f"缺少任务配置: {task_toml}")

    config: dict[str, Any] = tomllib.loads(project_toml.read_text(encoding="utf-8"))
    config.update(tomllib.loads(task_toml.read_text(encoding="utf-8")))
    return config.get("build_image", DEFAULT_BUILD_IMAGE), config


def resolve_acr_reference(
    task_path: str,
    source_image: str,
    mapping: dict[str, Any],
) -> str:
    """把官方镜像引用换成 ACR digest 引用；任务精确匹配优先。"""
    task_images = mapping.get("task_images", {})
    entry = task_images.get(task_path)
    if isinstance(entry, dict) and entry.get("acr"):
        return entry["acr"]
    acr = (mapping.get("source_to_acr") or {}).get(source_image)
    if acr:
        return acr
    raise FreezeError(
        f"任务 {task_path} 的镜像 {source_image} 在映射中没有 ACR 条目；"
        "请先补齐 image_mapping 或运行镜像迁移工具"
    )


def select_tasks(cfg: ShellConfig) -> tuple[list[str], dict[str, int]]:
    """按配置筛选最终任务清单：包含/排除/去重/随机排序。"""
    official = load_official_task_list(cfg.source_task_file)
    excluded = load_completed_exclusions(cfg.exclude_completed_task_file)
    include_projects = set(cfg.include_projects)
    include_tasks = set(cfg.include_tasks)

    kept: list[str] = []
    dropped = {
        "official_total": len(official),
        "excluded_completed": 0,
        "filtered_include": 0,
        "missing_assets": 0,
    }
    for task in official:
        if task in excluded:
            dropped["excluded_completed"] += 1
            continue
        if include_tasks and task not in include_tasks:
            dropped["filtered_include"] += 1
            continue
        project = task.split("/", 1)[0]
        if include_projects and project not in include_projects:
            dropped["filtered_include"] += 1
            continue
        kept.append(task)

    # 数据与脚本缺失的任务在冻结阶段剔除并计数；preflight 汇总报告。
    runnable: list[str] = []
    for task in kept:
        if _task_inputs_present(cfg, task):
            runnable.append(task)
        else:
            dropped["missing_assets"] += 1

    if not runnable:
        raise FreezeError(
            "筛选后没有可运行任务；请检查 DATASET_DIR / SOURCE_TASK_FILE / 排除清单"
        )

    if cfg.shuffle_tasks:
        random.Random(cfg.shuffle_seed).shuffle(runnable)
    return runnable, dropped


def _task_inputs_present(cfg: ShellConfig, task: str) -> bool:
    project, task_name = task.split("/", 1)
    task_dir = cfg.repo_dir / "projects" / project / task_name
    if not (task_dir / "config.toml").exists():
        return False
    data_dir = cfg.dataset_projects_dir / project / task_name
    return (data_dir / "src.tgz").exists()


def freeze_inputs(cfg: ShellConfig, tasks: list[str]) -> dict[str, Any]:
    """把最终任务清单、脚本、TOML 与数据资产冻结到 run_scope/frozen。

    大文件（src.tgz）按 DATA_FREEZE_MODE 硬链接或复制；硬链接不是内容
    不可变保障，恢复前会按 manifest 中记录的 hash 核对源资产未变。
    """
    mapping = load_image_mapping(cfg.image_mapping_file)
    frozen_projects = cfg.frozen_dir / "projects"
    frozen_dataset = cfg.frozen_dir / "dataset"
    entries: list[TaskEntry] = []

    for task in tasks:
        project, task_name = task.split("/", 1)
        source_image, merged_config = merge_build_image(cfg.repo_dir, task)
        acr_ref = resolve_acr_reference(task, source_image, mapping)

        # 冻结项目/任务脚本（保持相对布局）。
        src_project_dir = cfg.repo_dir / "projects" / project
        dst_project_dir = frozen_projects / project
        util.hardlink_or_copy(
            src_project_dir / "project.toml", dst_project_dir / "project.toml", mode="copy"
        )
        src_task_dir = src_project_dir / task_name
        dst_task_dir = dst_project_dir / task_name
        dst_task_dir.mkdir(parents=True, exist_ok=True)
        for item in sorted(src_task_dir.iterdir()):
            if item.is_file():
                util.hardlink_or_copy(item, dst_task_dir / item.name, mode="copy")

        # 冻结数据资产（大文件按策略硬链接/复制）。
        src_data_dir = cfg.dataset_projects_dir / project / task_name
        dst_data_dir = frozen_dataset / project / task_name
        data_hashes: dict[str, str] = {}
        for data_file in TASK_DATA_FILES:
            source = src_data_dir / data_file
            if source.exists():
                util.hardlink_or_copy(source, dst_data_dir / data_file, mode=cfg.data_freeze_mode)
                data_hashes[data_file] = util.sha256_file(dst_data_dir / data_file)

        entries.append(
            TaskEntry(
                task_path=task,
                project=project,
                task_name=task_name,
                build_image_source=source_image,
                build_image_acr=acr_ref,
                data_files=data_hashes,
            )
        )
        _ = merged_config  # 合并结果已体现在 source_image；配置脱敏在 workspace 阶段做

    return _build_manifest_payload(cfg, mapping, entries)


def _build_manifest_payload(
    cfg: ShellConfig, mapping: dict[str, Any], entries: list[TaskEntry]
) -> dict[str, Any]:
    fingerprint = cfg.core_fingerprint()
    payload = {
        "schema_version": 1,
        "created_at": util.utc_now_iso(),
        "scope": cfg.scope,
        "fingerprint": fingerprint,
        "stage_policy": cfg.stage_policy.to_manifest(),
        "task_selection": {
            "shuffle_tasks": cfg.shuffle_tasks,
            "shuffle_seed": cfg.shuffle_seed,
            "data_freeze_mode": cfg.data_freeze_mode,
            "task_count": len(entries),
        },
        "tasks": [
            {
                "task_path": e.task_path,
                "build_image_source": e.build_image_source,
                "build_image_acr": e.build_image_acr,
                "data_files": e.data_files,
            }
            for e in entries
        ],
        "image_mapping": {
            "file": str(cfg.image_mapping_file),
            "updated_at": mapping.get("updated_at"),
            "complete": mapping.get("complete"),
        },
        "toolchain": {
            "capture_proxy": {
                "dir": str(cfg.capture_proxy_dir),
                "entrypoint": str(cfg.capture_proxy_entrypoint),
                "file_hashes": _capture_tool_fingerprints(cfg),
            },
        },
    }
    # fingerprint 子树不做脱敏：它只含参数、路径与文件 hash（core_fingerprint
    # 从不包含凭据），脱敏会破坏续跑核对的精确比较。其余字段全部脱敏。
    payload = scrub_secrets(payload)
    payload["fingerprint"] = fingerprint
    return payload


def _capture_tool_fingerprints(cfg: ShellConfig) -> dict[str, str]:
    """冻结所用 capture 工具的代码指纹；包含实际依赖，不只入口文件。"""
    names = (
        "proxy_retry.py",
        "proxy.py",
        "capture_core.py",
        "finalize.py",
        "finalize-proc-split.py",
        "export_sharegpt_recovered.py",
    )
    hashes: dict[str, str] = {}
    for name in names:
        path = cfg.capture_proxy_dir / name
        if path.exists():
            hashes[name] = util.sha256_file(path)
    if "proxy_retry.py" not in hashes:
        raise FreezeError(f"capture 代理入口不存在: {cfg.capture_proxy_dir}/proxy_retry.py")
    return hashes


def write_manifest(cfg: ShellConfig, payload: dict[str, Any]) -> Path:
    """manifest 原子落盘；manifest 中不含凭据。"""
    path = cfg.manifest_path()
    util.atomic_write_json(path, payload)
    return path


def load_manifest(cfg: ShellConfig) -> dict[str, Any]:
    return util.read_json(cfg.manifest_path())


def verify_manifest_for_resume(cfg: ShellConfig) -> dict[str, Any]:
    """同 EXP 恢复前的核对：核心指纹必须一致，数据资产 hash 必须未变。

    返回核对报告；不一致时抛异常，禁止混入旧实验。
    """
    manifest = load_manifest(cfg)
    stored = manifest.get("fingerprint") or {}
    current = cfg.core_fingerprint()

    mismatched = []
    for key in ("stage_policy", "model", "reasoning_effort", "claude_code_version",
                "model_provider", "agent", "prompt_style", "task_repeat_n",
                "agent_timeout_seconds", "agent_max_output_tokens", "agent_memory",
                "validation_memory", "agent_cpus", "validation_cpus",
                "repo_commit", "repo_script_fingerprints", "image_mapping_hash",
                "source_task_file_hash"):
        if stored.get(key) != current.get(key):
            mismatched.append(key)

    asset_mismatch = _verify_frozen_assets(cfg, manifest)

    if mismatched or asset_mismatch:
        raise FreezeError(
            "冻结输入与当前配置不一致，拒绝续跑；需要新实验（新 EXP 或 ROUND_NUMBER）。"
            f" 参数差异: {mismatched}；资产差异: {asset_mismatch[:5]}"
        )
    return {
        "verified": True,
        "task_count": len(manifest.get("tasks", [])),
        "fingerprint_matches": True,
    }


def _verify_frozen_assets(cfg: ShellConfig, manifest: dict[str, Any]) -> list[str]:
    """逐任务核对冻结资产的 hash；硬链接指向的源若被改动会在此暴露。"""
    problems: list[str] = []
    for task in manifest.get("tasks", []):
        project, task_name = task["task_path"].split("/", 1)
        data_dir = cfg.frozen_dir / "dataset" / project / task_name
        for name, expected in (task.get("data_files") or {}).items():
            frozen_file = data_dir / name
            if not frozen_file.exists():
                problems.append(f"{task['task_path']}:{name}:missing")
                continue
            if util.sha256_file(frozen_file) != expected:
                problems.append(f"{task['task_path']}:{name}:hash_changed")
    return problems


def prepare_new_experiment(cfg: ShellConfig) -> dict[str, Any]:
    """新实验：冻结输入并写 manifest；返回冻结报告。"""
    if cfg.manifest_path().exists():
        raise FreezeError(f"manifest 已存在，不能重复冻结: {cfg.manifest_path()}")
    cfg.derive()
    tasks, dropped = select_tasks(cfg)
    payload = freeze_inputs(cfg, tasks)
    payload["task_selection"]["dropped"] = dropped
    write_manifest(cfg, payload)
    return payload
