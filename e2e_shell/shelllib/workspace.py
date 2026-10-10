"""官方 setup_workspace 的可审计适配版本。

保留官方准备顺序：镜像残留清理 → 目录创建 → 验证依赖安装 →
validate.py 注入 → 源码解包 → patch-only 输入 → 构建脚本 → 配置脱敏 →
目标仓库 Git 清理 → pre-patches → 可信 prepare → /src_backup 快照。

差异（均为外壳职责，不改变官方语义）：
- 容器操作走 docker_control（labels、资源、启动恢复、流式输出）；
- 输入来自冻结目录而非官方仓库原位；
- Arvo/CyberGym 残留清理依据冻结元数据中的原始镜像引用判定，
  不因 ACR 私有 alias 不含 arvo/cybergym 而漏掉清理；
- 可信 prepare 提前完成后，最终阶段验证不再重复 --run-prepare。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .docker_control import ContainerSpec, DockerControl

PREPARE_TIMEOUT = 1800  # 官方 prepare.sh 内部限额，保留不改

def dumps_minimal_toml(data: dict[str, Any]) -> str:
    """窄域 TOML 序列化：str / list[str] / int / float / bool。

    与官方 tomli_w.dumps 输出同构；只用于写容器内 /config/config.toml。
    """

    def escape(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )

    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key} = {value}")
        elif isinstance(value, str):
            lines.append(f'{key} = "{escape(value)}"')
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            if not value:
                lines.append(f"{key} = []")
            else:
                items = ", ".join(f'"{escape(v)}"' for v in value)
                lines.append(f"{key} = [{items}]")
        else:
            raise ValueError(f"不支持的 TOML 值类型: {key}={value!r}")
    return "\n".join(lines) + "\n"

@dataclass
class WorkspaceInputs:
    """一次 workspace 准备所需的冻结输入。"""

    task_path: str  # project/task_name
    mode: str
    frozen_dir: Path
    repo_dir: Path  # 官方仓库（install_validate_deps.sh 等运行所需文件）
    build_image_source: str  # 冻结元数据中的原始镜像引用
    copy_gt_poc: bool = True

    @property
    def project(self) -> str:
        return self.task_path.split("/", 1)[0]

    @property
    def task_name(self) -> str:
        return self.task_path.split("/", 1)[1]

    @property
    def script_dir(self) -> Path:
        return self.frozen_dir / "projects" / self.project / self.task_name

    @property
    def project_toml_path(self) -> Path:
        return self.frozen_dir / "projects" / self.project / "project.toml"

    @property
    def data_dir(self) -> Path:
        return self.frozen_dir / "dataset" / self.project / self.task_name

    def merged_config(self) -> dict[str, Any]:
        config: dict[str, Any] = tomllib.loads(
            self.project_toml_path.read_text(encoding="utf-8")
        )
        config.update(
            tomllib.loads((self.script_dir / "config.toml").read_text(encoding="utf-8"))
        )
        return config

class WorkspaceError(RuntimeError):
    pass

@dataclass
class WorkspaceSetup:
    """适配后的官方工作区准备流程。"""

    docker: DockerControl
    inputs: WorkspaceInputs
    prepare_env: dict[str, str] = field(default_factory=dict)

    def setup(self, container: str, *, run_prepare: bool) -> dict[str, Any]:
        """按官方顺序准备容器工作区；返回清理与 prepare 的记录。"""
        record: dict[str, Any] = {"arvo_residue_cleaned": False, "prepare_ran": False}
        config = self.inputs.merged_config()
        repo_to_patch = config.get("repo_to_patch", "")

        # 1) 官方镜像残留清理：依据原始镜像引用（n132/arvo、cybergym 等）。
        source_image = (self.inputs.build_image_source or "").lower()
        if "arvo" in source_image or "cybergym" in source_image:
            code, out, err = self.docker.exec_run(
                container,
                "find /src -mindepth 1 -maxdepth 1 ! -name honggfuzz ! -name libfuzzer "
                "! -name afl ! -name aflplusplus ! -name fuzztest -exec rm -rf {} +",
                timeout=300,
            )
            self._check(code, "arvo residue find", out, err, required=False)
            self.docker.exec_run(
                container, "rm -rf /out /work /usr/bin/arvo", timeout=300
            )
            self.docker.exec_run(
                container, "find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} +", timeout=300
            )
            self.docker.exec_run(container, "mkdir -p /out /work", timeout=60)
            record["arvo_residue_cleaned"] = True

        # 2) 目录
        code, out, err = self.docker.exec_run(
            container, "mkdir -p /config /data /output /scripts /logs", timeout=60
        )
        self._check(code, "mkdir", out, err)

        # 3) 验证依赖（官方 install_validate_deps.sh + sudo/git）
        code, out, err = self.docker.exec_run(
            container,
            "apt-get update -qq && apt-get install -y -qq sudo git >/dev/null 2>&1",
            timeout=900,
            env=self.prepare_env or None,
        )
        self._check(code, "apt-get sudo git", out, err, required=False)
        deps = self.inputs.repo_dir / "scripts" / "install_validate_deps.sh"
        if not deps.exists():
            raise WorkspaceError(f"官方仓库缺少 install_validate_deps.sh: {deps}")
        self.docker.copy_to(container, deps, "/install_validate_deps.sh")
        code, out, err = self.docker.exec_run(
            container, "bash -eux /install_validate_deps.sh", timeout=1800,
            env=self.prepare_env or None,
        )
        self._check(code, "install_validate_deps", out, err)

        # 4) 注入冻结的官方 validate.py
        validate_py = self.inputs.repo_dir / "scripts" / "validate.py"
        if not validate_py.exists():
            raise WorkspaceError(f"官方仓库缺少 validate.py: {validate_py}")
        self.docker.copy_to(container, validate_py, "/scripts/validate.py")

        # 5) 源码解包
        src_tgz = self.inputs.data_dir / "src.tgz"
        if not src_tgz.exists():
            raise WorkspaceError(f"冻结数据缺少 src.tgz: {src_tgz}")
        self.docker.copy_to(container, src_tgz, "/src/src.tgz")
        code, out, err = self.docker.exec_run(
            container, "tar xf /src/src.tgz -C /src && rm /src/src.tgz", timeout=1800
        )
        self._check(code, "tar extract", out, err)

        # 6) patch-only 输入
        if self.inputs.mode == "patch-only":
            for name in ("crash.log", "poc.bin"):
                source = self.inputs.data_dir / name
                if source.exists():
                    self.docker.copy_to(container, source, f"/src/{name}")
            self.inputs.copy_gt_poc = True

        # 7) 构建脚本
        for script in ("prepare.sh", "compile.sh", "run_poc.sh", "test.sh"):
            script_file = self.inputs.script_dir / script
            if script_file.exists():
                self.docker.copy_to(container, script_file, f"/src/{script}")

        # 8) 配置脱敏：容器内只保留 repo_to_patch 与 immutable_files。
        #    注意：当前 validate.py 并未用 immutable_files 强制过滤候选变更，
        #    此处只是沿用官方容器内配置约定。
        sanitized = {
            "repo_to_patch": repo_to_patch,
            "immutable_files": config.get("immutable_files", []),
        }
        toml_text = dumps_minimal_toml(sanitized)
        code, out, err = self.docker.exec_run(
            container,
            "cat > /config/config.toml << 'EOF'\n" + toml_text + "EOF",
            timeout=60,
        )
        self._check(code, "write config.toml", out, err)

        # 9) 目标仓库 Git 清理：官方注释明确仅目标仓库（依赖目录的 git
        #    会被构建系统重新使用，不能一刀切删除）。在 pre-patches、
        #    prepare 和快照之前执行，restore_src 无法把历史带回来。
        if repo_to_patch:
            self.docker.exec_run(
                container, f"rm -rf /src/{repo_to_patch}/.git", timeout=300
            )

        # 10) pre-patches
        pre_patches = list(config.get("pre_patches") or [])
        if not pre_patches and config.get("pre_patch"):
            pre_patches = [config["pre_patch"]]
        pre_patch_repo = f"/src/{repo_to_patch}" if repo_to_patch else "/src"
        for index, name in enumerate(pre_patches):
            patch_file = self.inputs.script_dir / name
            if not patch_file.exists():
                raise WorkspaceError(f"pre_patch 文件不存在: {patch_file}")
            container_path = f"/config/pre_patch_{index}.diff"
            self.docker.copy_to(container, patch_file, container_path)
            applied = False
            for strip_level in (0, 1, 2, 3):
                if strip_level == 0:
                    cmd = f"cd {pre_patch_repo} && git apply {container_path}"
                else:
                    cmd = f"cd {pre_patch_repo} && patch -p{strip_level} < {container_path}"
                code, _out, _err = self.docker.exec_run(container, cmd, timeout=600)
                if code == 0:
                    applied = True
                    break
            if not applied:
                raise WorkspaceError(
                    f"pre_patch 应用失败（strip 0-3 均失败）: {name}"
                )

        # 11) 参考 PoC（/data；agent 模式不复制，验证阶段需要）
        if self.inputs.copy_gt_poc:
            gt_poc = self.inputs.data_dir / "poc.bin"
            if gt_poc.exists():
                self.docker.copy_to(container, gt_poc, "/data/poc.bin")

        # 12) 可信 prepare + 快照（官方 timeout 保留）
        if run_prepare:
            code, out, err = self.docker.exec_run(
                container,
                "bash -eux /src/prepare.sh",
                timeout=PREPARE_TIMEOUT,
                workdir="/src",
                env=self.prepare_env or None,
            )
            self._check(code, "prepare.sh", out, err)
            code, out, err = self.docker.exec_run(
                container, "cp -a /src /src_backup", timeout=1800
            )
            self._check(code, "src_backup", out, err)
            record["prepare_ran"] = True

        return record

    @staticmethod
    def _check(code: int, stage: str, stdout: str, stderr: str, *, required: bool = True) -> None:
        if code != 0 and required:
            raise WorkspaceError(
                f"workspace 步骤失败: {stage} exit={code}\n"
                f"stdout[-1000:]: {stdout[-1000:]}\nstderr[-1000:]: {stderr[-1000:]}"
            )

def container_spec_for_role(
    *,
    base_spec_args: dict[str, Any],
    role: str,
    scope: str,
    attempt_id: str,
    launch_id: str,
    image: str,
    name: str,
    network: str | None,
    memory: str,
    cpus: float,
    pids_limit: int,
) -> ContainerSpec:
    """构造 agent/验证容器的统一 spec：auto_remove=False、资源限制、labels。"""
    return ContainerSpec(
        image=image,
        name=name,
        role=role,
        scope=scope,
        attempt_id=attempt_id,
        launch_id=launch_id,
        network=network,
        memory=memory,
        cpus=cpus,
        pids_limit=pids_limit,
        **base_spec_args,
    )
