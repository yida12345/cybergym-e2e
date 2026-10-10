"""Docker 生命周期控制：共享启动槽、命名恢复、资源限制与所有权 labels。

与现有协议共用 /tmp/cybergym-docker-startup-slots/slot-N.lock（默认 8 槽），
覆盖 pull、create、start 整个冷启动过程。Agent 和所有验证阶段都必须
使用同一入口。

超时不等于 daemon 没创建：重试前按确定名字查询，核对镜像、角色、
mounts、网络和资源全部匹配才恢复 start；未知同名对象拒绝复用。
不能无条件创建第二个随机名称容器。

实现基于 Docker CLI 子进程（可注入 runner，测试不需要真实 Docker）。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from . import util

CommandRunner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]


class DockerControlError(RuntimeError):
    pass


class UncertainContainerState(DockerControlError):
    """daemon 后台操作是否结束无法确认；阻止同 attempt 的第二次创建。"""


def default_runner(timeout_scale: float = 1.0) -> CommandRunner:
    def run(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout * timeout_scale + 5,
            errors="replace",
        )
        return completed

    return run


@dataclass
class ContainerSpec:
    """创建容器所需的全部参数；恢复前逐字段核对。"""

    image: str
    name: str
    role: str  # agent / stage1..4 / squid / network-test
    scope: str
    attempt_id: str | None = None
    launch_id: str | None = None
    command: list[str] = field(default_factory=lambda: ["sleep", "infinity"])
    network: str | None = None
    workdir: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    memory: str | None = None
    cpus: float | None = None
    pids_limit: int | None = None
    core_limit_bytes: int | None = None
    storage_opt_size: str | None = None  # 存储驱动支持时的可写层硬限额
    volumes: dict[str, dict[str, str]] = field(default_factory=dict)

    def ownership_labels(self) -> dict[str, str]:
        labels = {
            "cybergym_e2e.managed": "1",
            "cybergym_e2e.scope": self.scope,
            "cybergym_e2e.role": self.role,
        }
        if self.attempt_id:
            labels["cybergym_e2e.attempt_id"] = self.attempt_id
        if self.launch_id:
            labels["cybergym_e2e.launch_id"] = self.launch_id
        labels.update(self.labels)
        return labels

    def to_record(self) -> dict[str, Any]:
        """创建前记录的完整参数；daemon 超时后按此核对。"""
        return {
            "image": self.image,
            "name": self.name,
            "role": self.role,
            "scope": self.scope,
            "attempt_id": self.attempt_id,
            "launch_id": self.launch_id,
            "command": list(self.command),
            "network": self.network,
            "workdir": self.workdir,
            "env_keys": sorted(self.env),
            "labels": self.ownership_labels(),
            "memory": self.memory,
            "cpus": self.cpus,
            "pids_limit": self.pids_limit,
            "core_limit_bytes": self.core_limit_bytes,
            "storage_opt_size": self.storage_opt_size,
            "volumes": {k: dict(v) for k, v in self.volumes.items()},
        }


@contextlib.contextmanager
def docker_startup_slot(lock_dir: Path, concurrency: int) -> Iterator[int]:
    """整机启动槽：与 ExploitGym/run_arc 共用同一 flock 协议。

    覆盖 pull/create/start；拿不到槽位时每秒重试，不设内部超时
    （预算由调用方的 setup 超时控制）。
    """
    if concurrency < 1:
        raise ValueError("startup concurrency must be positive")
    lock_dir = Path(lock_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)
    while True:
        for slot_index in range(concurrency):
            lock_file = (lock_dir / f"slot-{slot_index}.lock").open("a+")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_file.close()
                continue
            try:
                yield slot_index
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
            return
        time.sleep(1)


@dataclass
class DockerControl:
    """容器生命周期控制。所有创建走启动槽。"""

    runner: CommandRunner = field(default_factory=default_runner)
    api_timeout: float = 600.0
    timeout_retries: int = 3
    retry_delay_seconds: float = 10.0
    startup_lock_dir: Path = Path("/tmp/cybergym-docker-startup-slots")
    startup_concurrency: int = 8
    popen: Any = None  # 注入的 Popen 工厂（默认 subprocess.Popen）；测试用

    def _popen_factory(self):
        return self.popen if self.popen is not None else subprocess.Popen

    def _run(self, command: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        return self.runner(command, timeout if timeout is not None else self.api_timeout)

    # ---- 基础查询 -------------------------------------------------------

    def inspect(self, reference: str) -> dict[str, Any] | None:
        result = self._run(["docker", "inspect", reference])
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise DockerControlError(f"docker inspect 输出不是 JSON: {reference}") from exc
        return payload[0] if isinstance(payload, list) and payload else None

    def image_id(self, image: str) -> str | None:
        result = self._run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def image_exists(self, image: str) -> bool:
        return self.image_id(image) is not None

    def pull(self, image: str) -> str:
        """在启动槽内拉取镜像；返回 image ID。"""
        with docker_startup_slot(self.startup_lock_dir, self.startup_concurrency):
            result = self._run(["docker", "pull", image])
        if result.returncode != 0:
            raise DockerControlError(
                f"docker pull 失败: {image}: {(result.stderr or result.stdout).strip()[-500:]}"
            )
        image_id = self.image_id(image)
        if image_id is None:
            raise DockerControlError(f"镜像拉取后仍不可见: {image}")
        return image_id

    # ---- 容器创建/启动与恢复 -------------------------------------------

    def _create_command(self, spec: ContainerSpec) -> list[str]:
        command = ["docker", "create"]
        command += ["--name", spec.name]
        if spec.network:
            command += ["--network", spec.network]
        if spec.workdir:
            command += ["-w", spec.workdir]
        for key, value in spec.ownership_labels().items():
            command += ["--label", f"{key}={value}"]
        for key, value in spec.env.items():
            if value is not None:
                command += ["-e", f"{key}={value}"]
        if spec.memory:
            command += ["--memory", spec.memory, "--memory-swap", spec.memory]
        if spec.cpus:
            command += ["--cpus", str(spec.cpus)]
        if spec.pids_limit is not None:
            command += ["--pids-limit", str(spec.pids_limit)]
        if spec.core_limit_bytes is not None:
            command += ["--ulimit", f"core={spec.core_limit_bytes}"]
        else:
            command += ["--ulimit", "core=0"]  # 禁用 core dump
        if spec.storage_opt_size:
            command += ["--storage-opt", f"size={spec.storage_opt_size}"]
        for host, bind in spec.volumes.items():
            mode = bind.get("mode", "rw")
            command += ["-v", f"{host}:{bind['bind']}:{mode}" if mode != "rw" else f"{host}:{bind['bind']}"]
        # auto_remove=False：先保存产物、会话、日志和退出证据，再精确删除。
        command += [spec.image] + list(spec.command)
        return command

    def _matches_record(self, container_info: dict[str, Any], record: dict[str, Any]) -> tuple[bool, str]:
        """daemon 侧对象是否与创建记录一致（镜像、labels、网络、资源）。"""
        config = container_info.get("Config", {})
        actual_labels = config.get("Labels") or {}
        for key, value in record["labels"].items():
            if actual_labels.get(key) != value:
                return False, f"label mismatch: {key}"
        actual_image = (config.get("Image") or "").split("@")[0]
        expected_image = record["image"].split("@")[0]
        if expected_image not in actual_image and actual_image not in expected_image:
            # digest 引用时以 repo 名比较，再由调用方核对镜像 ID
            repo_expected = expected_image.rsplit("/", 1)[-1].split(":")[0]
            repo_actual = actual_image.rsplit("/", 1)[-1].split(":")[0]
            if repo_expected != repo_actual:
                return False, f"image mismatch: {actual_image} != {expected_image}"
        host_config = container_info.get("HostConfig") or {}
        if record.get("memory") and host_config.get("Memory", 0) <= 0:
            return False, "memory limit missing"
        if record.get("pids_limit") is not None and host_config.get("PidsLimit") in (None, -1, 0):
            return False, "pids limit missing"
        networks = (container_info.get("NetworkSettings") or {}).get("Networks") or {}
        if record.get("network") and record["network"] not in networks:
            return False, f"network missing: {record['network']}"
        mounts = host_config.get("Binds") or []
        for host, bind in (record.get("volumes") or {}).items():
            prefix = f"{host}:"
            if not any(m.startswith(prefix) for m in mounts):
                return False, f"mount missing: {host}"
        return True, ""

    def create_and_start(self, spec: ContainerSpec) -> dict[str, Any]:
        """启动槽内 create+start；超时后按名字恢复，不创建第二个容器。

        返回容器 inspect 信息。所有关键参数先记录，恢复前核对。
        """
        record = spec.to_record()
        total_attempts = self.timeout_retries + 1
        recover_by_name = False
        last_timeout: Exception | None = None

        with docker_startup_slot(self.startup_lock_dir, self.startup_concurrency):
            for attempt in range(1, total_attempts + 1):
                try:
                    container_info = self.inspect(spec.name)
                    if container_info is not None:
                        if recover_by_name:
                            matches, reason = self._matches_record(container_info, record)
                            if not matches:
                                raise DockerControlError(
                                    f"同名容器与创建记录不匹配，拒绝复用: {spec.name}: {reason}"
                                )
                        else:
                            raise DockerControlError(
                                f"容器名已存在且不是本次恢复: {spec.name}"
                            )
                    else:
                        result = self._run(self._create_command(spec))
                        if result.returncode != 0:
                            raise DockerControlError(
                                f"docker create 失败: {spec.name}: "
                                f"{(result.stderr or result.stdout).strip()[-500:]}"
                            )
                        container_info = self.inspect(spec.name)
                        if container_info is None:
                            raise DockerControlError(f"容器创建后不可见: {spec.name}")

                    status = container_info.get("State", {}).get("Status", "")
                    if status in ("created",):
                        start_result = self._run(["docker", "start", spec.name])
                        if start_result.returncode != 0:
                            raise DockerControlError(
                                f"docker start 失败: {spec.name}: "
                                f"{(start_result.stderr or start_result.stdout).strip()[-500:]}"
                            )
                        container_info = self.inspect(spec.name)
                        if container_info is None:
                            raise DockerControlError(f"容器启动后不可见: {spec.name}")
                    elif status in ("running", "restarting", "paused", "exited", "dead"):
                        if recover_by_name:
                            # 已退出但记录匹配：恢复 start
                            if status in ("exited", "dead"):
                                start_result = self._run(["docker", "start", spec.name])
                                if start_result.returncode != 0:
                                    raise DockerControlError(
                                        f"恢复启动失败: {spec.name}"
                                    )
                                container_info = self.inspect(spec.name)
                        else:
                            raise DockerControlError(
                                f"容器名已存在且状态为 {status}: {spec.name}"
                            )
                    else:
                        raise DockerControlError(
                            f"无法从状态 {status} 恢复容器: {spec.name}"
                        )
                    return container_info
                except subprocess.TimeoutExpired as exc:
                    last_timeout = exc
                    recover_by_name = True
                    # 槽位在 daemon 不确定窗口内保持持有（仍在 with 内），
                    # 避免过早释放造成实际启动数超限。
                    if attempt >= total_attempts:
                        break
                    time.sleep(self.retry_delay_seconds)
                except DockerControlError:
                    raise
        raise UncertainContainerState(
            f"Docker API 超时后无法确认容器状态: {spec.name}; "
            f"已核对 {self.timeout_retries + 1} 次; last={last_timeout}"
        )

    # ---- 执行与采集 -----------------------------------------------------

    def exec_run(
        self,
        container: str,
        command: str,
        *,
        timeout: float,
        env: dict[str, str] | None = None,
        workdir: str | None = None,
    ) -> tuple[int, str, str]:
        """容器内执行命令；返回 (exit_code, stdout, stderr)。"""
        cmd = ["docker", "exec"]
        for key, value in (env or {}).items():
            if value is not None:
                cmd += ["-e", f"{key}={value}"]
        if workdir:
            cmd += ["-w", workdir]
        cmd += [container, "timeout", str(int(timeout)), "bash", "-c", command]
        try:
            result = self._run(cmd, timeout)
        except subprocess.TimeoutExpired as exc:
            raise DockerControlError(
                f"exec 超时（宿主侧 timeout+10s 仍未返回）: {container}: {command[:120]}"
            ) from exc
        return result.returncode, result.stdout or "", result.stderr or ""

    def exec_stream_to_file(
        self,
        container: str,
        command: str,
        output_path: Path,
        *,
        timeout: float,
        env: dict[str, str] | None = None,
        workdir: str | None = None,
        poll_stop: Callable[[], str | None] | None = None,
        poll_interval: float = 5.0,
    ) -> tuple[int, str]:
        """流式执行并把 stdout/stderr 持续写入文件。

        与官方“结束后一次性写日志”不同：崩溃或手动停止前的输出
        已经在磁盘上。poll_stop 返回原因字符串时向进程组发 TERM。
        返回 (exit_code, stop_reason)。
        """
        cmd = ["docker", "exec"]
        for key, value in (env or {}).items():
            if value is not None:
                cmd += ["-e", f"{key}={value}"]
        if workdir:
            cmd += ["-w", workdir]
        cmd += [container, "bash", "-c", command]
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("wb") as handle:
            process = self._popen_factory()(
                cmd,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            reason: str | None = None
            start = time.monotonic()
            while process.poll() is None:
                if time.monotonic() - start > timeout:
                    reason = "exec_timeout"
                    util.terminate_process_tree(process.pid, grace_seconds=5.0)
                    break
                if poll_stop is not None:
                    reason = poll_stop()
                    if reason:
                        util.terminate_process_tree(process.pid, grace_seconds=5.0)
                        break
                time.sleep(poll_interval)
            returncode = process.wait()
        return returncode, reason or ""

    def exec_stream_lines(
        self,
        container: str,
        command: str,
        *,
        env: dict[str, str] | None = None,
        workdir: str | None = None,
    ) -> Any:
        """流式执行并返回 Popen（stdout 为行流）；由调用方逐行消费。

        CC 推理使用：输出边产生边被读取，持续落盘与 idle guard 由
        调用方（cc_runner）完成。
        """
        cmd = ["docker", "exec"]
        for key, value in (env or {}).items():
            if value is not None:
                cmd += ["-e", f"{key}={value}"]
        if workdir:
            cmd += ["-w", workdir]
        cmd += [container, "bash", "-c", command]
        return self._popen_factory()(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True
        )

    def copy_to(self, container: str, source: Path, destination: str) -> None:
        result = self._run(
            ["docker", "cp", str(source), f"{container}:{destination}"],
            timeout=300,
        )
        if result.returncode != 0:
            raise DockerControlError(
                f"docker cp 到容器失败: {source} -> {container}:{destination}: "
                f"{(result.stderr or result.stdout).strip()[-300:]}"
            )

    def copy_from(self, container: str, source: str, destination: Path) -> bool:
        """从容器取文件；不存在返回 False。"""
        result = self._run(
            ["docker", "cp", f"{container}:{source}", str(destination)],
            timeout=300,
        )
        return result.returncode == 0

    def save_exit_evidence(self, container: str, evidence_dir: Path) -> dict[str, Any]:
        """删除前保存 inspect（含 OOMKilled/ExitCode/时间）与日志。"""
        evidence_dir = Path(evidence_dir)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        info = self.inspect(container)
        if info is not None:
            util.atomic_write_json(evidence_dir / "inspect.json", info)
        logs = self._run(["docker", "logs", container], timeout=120)
        (evidence_dir / "container.log").write_text(
            (logs.stdout or "") + "\n--- stderr ---\n" + (logs.stderr or ""),
            encoding="utf-8",
            errors="replace",
        )
        if info is None:
            return {"available": False}
        state = info.get("State", {})
        return {
            "available": True,
            "status": state.get("Status"),
            "exit_code": state.get("ExitCode"),
            "oom_killed": state.get("OOMKilled"),
            "finished_at": state.get("FinishedAt"),
        }

    # ---- 删除与所有权 ---------------------------------------------------

    def remove_container(self, container: str, *, verify_labels: dict[str, str] | None = None) -> bool:
        """按所有权删除容器；labels 不匹配时拒绝。"""
        info = self.inspect(container)
        if info is None:
            return True
        if verify_labels:
            actual = (info.get("Config") or {}).get("Labels") or {}
            for key, value in verify_labels.items():
                if actual.get(key) != value:
                    raise DockerControlError(
                        f"拒绝删除：容器 {container} 的 label {key}={actual.get(key)!r} "
                        f"与预期 {value!r} 不匹配"
                    )
        result = self._run(["docker", "rm", "-f", container], timeout=180)
        if result.returncode != 0 and self.inspect(container) is not None:
            raise DockerControlError(
                f"容器删除失败: {container}: {(result.stderr or '').strip()[-300:]}"
            )
        return True

    def list_scope_containers(self, scope: str, *, role: str | None = None) -> list[str]:
        """列出本 scope 的容器；只按所有权 label 过滤。"""
        command = ["docker", "ps", "-aq", "--filter", f"label=cybergym_e2e.scope={scope}"]
        if role:
            command += ["--filter", f"label=cybergym_e2e.role={role}"]
        result = self._run(command, timeout=120)
        return result.stdout.split()

    def container_writable_bytes(self, containers: list[str]) -> int:
        """容器可写层大小合计（watchdog 用）。"""
        total = 0
        for container in containers:
            result = self._run(
                ["docker", "container", "inspect", "--size", "--format", "{{.SizeRw}}", container],
                timeout=120,
            )
            if result.returncode == 0:
                with contextlib.suppress(ValueError):
                    total += int(result.stdout.strip())
        return total

    def switch_network(self, container: str, from_network: str | None, to_network: str | None) -> None:
        """切换容器网络；to_network=None 表示完全断开（仅 loopback）。"""
        if from_network == to_network:
            return
        if from_network:
            self._run(["docker", "network", "disconnect", "-f", from_network, container], timeout=120)
        if to_network:
            result = self._run(["docker", "network", "connect", to_network, container], timeout=120)
            if result.returncode != 0:
                raise DockerControlError(
                    f"接入网络失败: {container} -> {to_network}: "
                    f"{(result.stderr or '').strip()[-300:]}"
                )

    def host_docker_info(self) -> dict[str, Any]:
        """当前资源与存储驱动（preflight 用；每次启动重新读取）。"""
        info = self.inspect("nonexistent")  # warm connection (also validates CLI exists)
        _ = info
        result = self._run(["docker", "info", "--format", "{{json .}}"], timeout=120)
        if result.returncode != 0:
            raise DockerControlError("docker info 失败")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise DockerControlError("docker info 输出不是 JSON") from exc

    def running_container_stats(self) -> list[dict[str, Any]]:
        """当前活动容器资源（共存预算用；配额不等于使用量，按最坏预算）。"""
        result = self._run(["docker", "ps", "--format", "{{json .}}"], timeout=120)
        containers = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            try:
                containers.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return containers
