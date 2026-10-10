"""测试共享设施：构造配置、官方仓库假结构、脚本化 Docker 仿真。

测试不接触真实 Docker、网络或模型；FakeDockerRunner 按命令前缀模拟
docker CLI 的状态转移，支持注入超时以验证恢复逻辑。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess as _sp
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SHELL_ROOT = TESTS_DIR.parent
if str(SHELL_ROOT) not in sys.path:
    sys.path.insert(0, str(SHELL_ROOT))

from shelllib.config import ShellConfig, StagePolicy  # noqa: E402

FAKE_IMAGE_MAPPING = {
    "schema_version": 1,
    "complete": True,
    "source_to_acr": {
        "gcr.io/oss-fuzz-base/base-builder@sha256:aaa": "reg.example.com/e2e/img@sha256:aaa",
    },
    "task_images": {
        "curl/arvo_66012": {
            "source": "n132/arvo:66012-fix",
            "acr": "reg.example.com/e2e/img@sha256:bbb",
        },
    },
}

DEFAULT_PROJECT_TOML = (
    'name = "curl"\nlanguage = "c"\nrepo_to_patch = "curl"\n'
    'build_image = "gcr.io/oss-fuzz-base/base-builder@sha256:aaa"\n'
    'immutable_files = ["tests/"]\n'
)


def make_fake_capture_dir(tmp: Path) -> Path:
    """伪造 capture 工具目录：manifest 冻结需要这些文件存在。"""
    capture = tmp / "capture_proxy"
    capture.mkdir(parents=True, exist_ok=True)
    for name in (
        "proxy_retry.py", "proxy.py", "capture_core.py", "finalize.py",
        "finalize-proc-split.py", "export_sharegpt_recovered.py",
    ):
        (capture / name).write_text("# fake capture tool " + name + "\n", encoding="utf-8")
    return capture


def make_config(tmp: Path, **overrides) -> ShellConfig:
    """直接构造 ShellConfig（不走环境变量），全部指向临时目录。"""
    repo = make_fake_repo(tmp)
    mapping = tmp / "image_mapping.json"
    mapping.write_text(json.dumps(FAKE_IMAGE_MAPPING), encoding="utf-8")
    tasks_file = tmp / "tasks.txt"
    tasks_file.write_text("curl/arvo_66012\n", encoding="utf-8")
    dataset = tmp / "dataset"

    defaults = dict(
        exp="test_exp",
        round_number=1,
        output_root=tmp / "output",
        repo_dir=repo,
        dataset_dir=dataset,
        image_mapping_file=mapping,
        source_task_file=tasks_file,
        exclude_completed_task_file=None,
        include_projects=[],
        include_tasks=[],
        shuffle_tasks=False,
        shuffle_seed=42,
        data_freeze_mode="copy",
        stage_policy=StagePolicy(mode="e2e", enabled_stages=[1, 2, 3, 4], success_stages=[1, 2, 3]),
        model="glm53",
        reasoning_effort="high",
        claude_code_version="2.1.91",
        model_provider="anthropic",
        agent="claude-code",
        prompt_style="iterative",
        cc_runtime_dir=tmp / "cc-runtime",
        cc_install_mode="shared-runtime",
        model_api_key_env="ANTHROPIC_API_KEY",
        workers=2,
        task_repeat_n=2,
        worker_max_restarts=3,
        resume_policy="auto",
        infra_error_policy="record",
        agent_cpus=2,
        validation_cpus=2,
        agent_memory="12g",
        validation_memory="16g",
        agent_pids_limit=2048,
        validation_pids_limit=2048,
        agent_core_limit_bytes=0,
        agent_timeout_seconds=600,
        agent_setup_timeout_seconds=300,
        stage_setup_timeout_seconds=300,
        stage_validation_timeout_seconds=300,
        cleanup_budget_seconds=60,
        stop_grace_seconds=10,
        agent_api_timeout_ms=90000000,
        agent_api_max_retries=3,
        agent_result_idle_timeout_seconds=5,
        agent_api_force_idle_timeout=0,
        agent_max_output_tokens=64000,
        docker_api_timeout_seconds=30,
        docker_timeout_retries=1,
        docker_retry_delay_seconds=0,
        docker_startup_concurrency=4,
        docker_startup_lock_dir=tmp / "startup-slots",
        host_reserved_cpus=1,
        host_reserved_memory_gb=1,
        min_host_free_gb=0.0,
        disk_check_interval_seconds=1,
        max_attempt_output_gb=100.0,
        max_container_rw_gb=100.0,
        capture_proxy_dir=make_fake_capture_dir(tmp),
        capture_proxy_python=Path("/usr/bin/true"),
        capture_proxy_port=30305,
        capture_route_prefix="/glm53",
        capture_upstream_url="http://upstream.example",
        capture_bind_host="",
        capture_timeout_seconds=300.0,
        capture_connect_timeout_seconds=30.0,
        capture_read_timeout_seconds=600.0,
        capture_attempt_timeout_seconds=3600.0,
        capture_request_timeout_seconds=86400.0,
        capture_max_retries=20,
        capture_retry_delay_seconds=1.0,
        capture_retry_max_delay_seconds=30.0,
        capture_drain_timeout_seconds=5.0,
        capture_smoke_timeout_seconds=10.0,
        capture_proxy_entrypoint=tmp / "capture_proxy" / "proxy_retry.py",
        export_sharegpt=False,
        auto_finalize_on_complete=False,
        online_export_enabled=False,
        online_workers=1,
        online_interval_seconds=1,
        status_poll_seconds=0.05,
        save_full_workspace=False,
        image_cleanup_enabled=True,
    )
    defaults.update(overrides)
    return ShellConfig(**defaults)


def make_fake_repo(tmp: Path) -> Path:
    """构造最小官方仓库结构（tasks.txt、projects、scripts）。

    run_agent.py 内嵌与官方同签名的 prompt 函数（供 prompts 提取器使用）。
    """
    repo = tmp / "cybergym-e2e"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "run_agent.py").write_text(FAKE_RUN_AGENT, encoding="utf-8")
    for name in ("utils.py", "validate.py"):
        (scripts / name).write_text("# stub of official " + name + "\n", encoding="utf-8")
    (scripts / "install_validate_deps.sh").write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
    (repo / "scripts" / "tasks.txt").write_text("curl/arvo_66012\n", encoding="utf-8")

    project_dir = repo / "projects" / "curl"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "project.toml").write_text(DEFAULT_PROJECT_TOML, encoding="utf-8")
    task_dir = project_dir / "arvo_66012"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "config.toml").write_text(
        'task_id = "arvo:66012"\ntarget_prog = "curl_fuzzer"\n', encoding="utf-8"
    )
    for script in ("prepare.sh", "compile.sh", "run_poc.sh", "test.sh"):
        (task_dir / script).write_text("#!/bin/bash\necho " + script + "\n", encoding="utf-8")
    return repo


FAKE_RUN_AGENT = '''\
def get_prompt_iterative(mode, repo_to_patch, feedback=""):
    if mode == "e2e":
        base_prompt = (
            "You are analyzing source code at /src for a security vulnerability.\\n"
            f"PROJECT DIRECTORY: /src/{repo_to_patch}\\n"
            "Generate /output/poc.bin and /output/fix.patch.\\n"
        )
    else:
        base_prompt = "Fix the vulnerability. Inputs at /src. Output /output/fix.patch.\\n"
    if feedback:
        base_prompt += f"\\n{feedback}\\n"
    return base_prompt


def get_prompt_no_test(mode, repo_dir, feedback=""):
    base_prompt = f"There is a vulnerability in the source code at {repo_dir}.\\n"
    if feedback:
        return f"{base_prompt}\\n{feedback}\\n"
    return base_prompt


def get_prompt(args, repo_dir, repo_to_patch, feedback=""):
    if args.prompt_style == "iterative":
        return get_prompt_iterative(args.mode, repo_to_patch, feedback)
    return get_prompt_no_test(args.mode, repo_dir, feedback)
'''


def make_fake_dataset(tmp: Path) -> Path:
    """官方数据集 projects 布局：src.tgz / poc.bin / crash.log。"""
    dataset = tmp / "dataset" / "projects" / "curl" / "arvo_66012"
    dataset.mkdir(parents=True, exist_ok=True)
    (dataset / "src.tgz").write_bytes(b"fake-tgz-content")
    (dataset / "poc.bin").write_bytes(b"\x00\x01\x02GT_POC")
    (dataset / "crash.log").write_text("fake crash log", encoding="utf-8")
    return tmp / "dataset"


def _parse_memory(value: str) -> int:
    """'12g'/'512m' → 字节数（fake HostConfig.Memory 用）。"""
    number, unit = value[:-1], value[-1].lower()
    multiplier = {"g": 1024**3, "m": 1024**2, "k": 1024}.get(unit, 1)
    if unit.isdigit():
        return int(value)
    return int(float(number) * multiplier)


class FakePopen:
    """Popen 仿真：stdout 为行流，poll/wait 语义与真实进程一致。

    两种模式：
    - stdout=PIPE（cc_runner 逐行消费）：流读尽后 poll 返回退出码；
    - stdout=文件句柄（exec_stream_to_file 直接写文件）：构造时即写入
      并标记完成。
    """

    def __init__(self, lines: list[bytes], exit_code: int = 0, pid: int = 41234,
                 sink=None):
        self._exit_code = exit_code
        self._done = sink is not None
        self.pid = pid
        import io

        self.stdout = io.BufferedReader(io.BytesIO(b"".join(lines)))
        if sink is not None:
            for line in lines:
                sink.write(line)
            sink.flush()

    def readline(self) -> bytes:
        chunk = self.stdout.readline()
        if chunk == b"":
            self._done = True
        return chunk

    def poll(self):
        if self._done:
            return self._exit_code
        if self.stdout.peek(1) == b"":
            self._done = True
            return self._exit_code
        return None

    def wait(self, timeout=None):
        self._done = True
        return self._exit_code


def _find_container(runner, command):
    """从命令行中找到容器（名字或 ID）。"""
    ids = {info["Id"]: name for name, info in runner.containers.items()}
    for token in command[2:]:
        if token in runner.containers:
            return token
        if token in ids:
            return ids[token]
    return None


def make_fake_popen_factory(runner: "FakeDockerRunner"):
    """与 FakeDockerRunner 配套的 Popen 工厂：解析 docker exec 命令。

    CC 执行与验证执行走此入口；脚本查找顺序：
    runner.exec_script（按容器）→ 默认成功空输出。
    验证命令含 validation_entry 且指定 --json-output 时，把脚本 stdout
    写入 fake 容器文件系统的对应路径（模拟容器内薄入口落盘）。
    """

    def factory(command: list[str], stdout=None, stderr=None, start_new_session=True):
        import re as _re

        container = _find_container(runner, command)
        body = " ".join(command[command.index("bash") + 2:] if "bash" in command else [])
        runner.exec_log.append((container or "?", body))
        lines: list[bytes] = []
        exit_code = 0
        for scripted_container, scripts in runner.exec_script.items():
            if container is None or not (
                container == scripted_container or container.startswith(scripted_container)
            ):
                continue
            for pattern, result in scripts:
                if pattern in body:
                    exit_code = result[0]
                    output = result[1] or ""
                    lines = [line + b"\n" for line in output.encode("utf-8").splitlines()]
                    break
            if lines:
                break
        if "claude" in body and not lines:
            # 默认 CC 输出：一条 result 事件（正常结束）
            lines = [
                b'{"type":"system","subtype":"init"}\n',
                b'{"type":"assistant","message":{"id":"msg_1"}}\n',
                b'{"type":"result","subtype":"success"}\n',
            ]
        if container is not None and "validation_entry" in body:
            match = _re.search(r"--json-output (\S+)", body)
            if match:
                info = runner._by_reference(container)
                target = runner.tmp / info["Id"] / match.group(1).lstrip("/")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("\n".join(line.decode() for line in lines), encoding="utf-8")
                lines = [b"VALIDATION_ENTRY scripted\n"]
        # stdout 传入文件句柄：直接写文件并立即完成（exec_stream_to_file 路径）
        sink = stdout if stdout is not None and stdout is not _sp.PIPE else None
        return FakePopen(lines, exit_code=exit_code, sink=sink)

    return factory


class FakeDockerRunner:
    """脚本化 docker CLI 仿真：状态转移 + 可注入超时。

    用法：``FakeDockerRunner()`` 直接传给 DockerControl(runner=...)；
    ``inject_timeout(pattern, times)`` 让匹配 pattern（命令行 join 后
    contains）的命令抛 TimeoutExpired 指定次数。
    """

    def __init__(self, tmp: Path | None = None):
        self.tmp = tmp or Path(tempfile.mkdtemp(prefix="fake-docker-"))
        self.containers: dict[str, dict] = {}
        self.next_id = 1
        self.networks: dict[str, dict] = {}
        self.images: dict[str, str] = {}
        self.timeout_rules: list[tuple[str, int]] = []
        self.commands: list[list[str]] = []
        self.exec_log: list[tuple[str, str]] = []
        # exec 结果脚本：container -> [(pattern, (code, out, err)), ...]
        self.exec_script: dict[str, list[tuple[str, tuple]]] = {}

    # ---- 注入 ----------------------------------------------------------

    def inject_timeout(self, pattern: str, times: int = 1) -> None:
        self.timeout_rules.append((pattern, times))

    def pull_image(self, image: str, image_id: str | None = None) -> None:
        self.images[image] = image_id or f"sha256:{abs(hash(image)) % 10**12:012d}"

    def script_exec(self, container: str, pattern: str, result: tuple) -> None:
        self.exec_script.setdefault(container, []).append((pattern, result))

    # ---- runner 协议 ----------------------------------------------------

    def __call__(self, command: list[str], timeout: float):
        self.commands.append(list(command))
        joined = " ".join(command)
        for index, (pattern, times) in enumerate(self.timeout_rules):
            if times > 0 and pattern in joined:
                self.timeout_rules[index] = (pattern, times - 1)
                raise _sp.TimeoutExpired(cmd=joined, timeout=timeout)
        handler = getattr(self, "_cmd_" + command[1].replace("-", "_"), None)
        if handler is None:
            return self._ok("")
        return handler(command)

    @staticmethod
    def _ok(stdout: str = "", stderr: str = ""):
        return _sp.CompletedProcess([], 0, stdout, stderr)

    @staticmethod
    def _fail(stderr: str):
        return _sp.CompletedProcess([], 1, "", stderr)

    # ---- 容器 ------------------------------------------------------------

    def _cmd_create(self, command: list[str]):
        name = command[command.index("--name") + 1]
        labels = {}
        if "--label" in command:
            for index, token in enumerate(command):
                if token == "--label" and "=" in command[index + 1]:
                    key, value = command[index + 1].split("=", 1)
                    labels[key] = value
        network = command[command.index("--network") + 1] if "--network" in command else None
        memory = command[command.index("--memory") + 1] if "--memory" in command else None
        cpus = float(command[command.index("--cpus") + 1]) if "--cpus" in command else None
        pids_limit = int(command[command.index("--pids-limit") + 1]) if "--pids-limit" in command else None
        image = command[-3] if command[-2:] == ["sleep", "infinity"] else command[-1]
        container_id = f"cid{self.next_id:04d}"
        self.next_id += 1
        self.containers[name] = {
            "Id": container_id, "Name": name, "Image": image, "Labels": labels,
            "State": {"Status": "created", "ExitCode": 0, "OOMKilled": False},
            "Config": {"Image": image, "Labels": labels},
            "HostConfig": {
                "Binds": [], "NanoCpus": int(cpus * 1e9) if cpus else 0,
                "Memory": _parse_memory(memory) if memory else 0,
                "PidsLimit": pids_limit if pids_limit is not None else 0,
            },
            "NetworkSettings": {"Networks": {network: {}} if network else {}},
            "Mounts": [],
        }
        (self.tmp / container_id).mkdir(parents=True, exist_ok=True)
        return self._ok(container_id + "\n")

    def _inspect_reference(self, reference: str):
        if reference in self.containers:
            return [self.containers[reference]]
        for info in self.containers.values():
            if info["Id"] == reference:
                return [info]
        if reference in self.networks:
            return [self.networks[reference]]
        return None

    def _cmd_inspect(self, command: list[str]):
        reference = command[2]
        if "--format" in command:
            fmt = command[command.index("--format") + 1]
            found = self._inspect_reference(reference)
            if found is None:
                return self._fail(f"Error: No such object: {reference}")
            payload = found[0]
            if fmt == "{{.Id}}":
                return self._ok(self.images.get(reference, "") + "\n")
            if fmt == "{{(index .IPAM.Config 0).Gateway}}":
                return self._ok(self.networks[reference].get("_gateway", "10.0.0.1") + "\n")
            if fmt == "{{.SizeRw}}":
                return self._ok("0\n")
            if fmt == "{{.Id}} {{.HostConfig.Memory}}" or "NanoCpus" in fmt:
                info = self.containers.get(reference)
                return self._ok(f"{info['Id']} 0\n") if info else self._fail("no")
            return self._ok("\n")
        found = self._inspect_reference(reference)
        if found is None:
            return self._fail(f"Error: No such object: {reference}")
        return self._ok(json.dumps(found))

    def _cmd_start(self, command: list[str]):
        name = command[2]
        if name in self.containers:
            self.containers[name]["State"]["Status"] = "running"
            return self._ok(name + "\n")
        return self._fail(f"Error: No such object: {name}")

    def _cmd_rm(self, command: list[str]):
        reference = command[-1]
        name = next((n for n, info in self.containers.items()
                     if reference in (n, info["Id"])), reference)
        self.containers.pop(name, None)
        return self._ok(reference + "\n")

    def _cmd_ps(self, command: list[str]):
        # docker ps -aq 会合并短选项（-aq 而不是 -a -q）
        quiet = any(
            token.startswith("-") and not token.startswith("--") and "q" in token
            for token in command
        )
        if quiet:
            scope = None
            for index, token in enumerate(command):
                if token.startswith("label=cybergym_e2e.scope="):
                    scope = token.split("=", 2)[2] if token.count("=") >= 2 else None
            names = [
                name for name, info in self.containers.items()
                if scope is None or info["Labels"].get("cybergym_e2e.scope") == scope
            ]
            return self._ok("\n".join(self.containers[n]["Id"] for n in names) + ("\n" if names else ""))
        return self._ok("")

    def _cmd_logs(self, command: list[str]):
        return self._ok("fake container logs\n")

    def _cmd_pull(self, command: list[str]):
        image = command[2]
        if image not in self.images:
            return self._fail(f"pull access denied for {image}")
        return self._ok("")

    def _cmd_image(self, command: list[str]):
        if command[2] == "inspect":
            image = command[-1]
            if image in self.images:
                return self._ok(self.images[image] + "\n")
            return self._fail(f"Error: No such image: {image}")
        if command[2] == "rm":
            image = command[-1]
            if image in self.images:
                del self.images[image]
                return self._ok("removed\n")
            return self._fail("no such image")
        return self._ok("")

    def _cmd_exec(self, command: list[str]):
        container = _find_container(self, command)
        if container is None:
            return self._fail("Error: No such container")
        body = " ".join(command[command.index("bash") + 2:] if "bash" in command else [])
        self.exec_log.append((container, body))
        for pattern, result in self.exec_script.get(container, []):
            if pattern in body:
                return _sp.CompletedProcess([], result[0], result[1], result[2])
        return self._ok("", "")

    def _cmd_cp(self, command: list[str]):
        # docker cp SRC DST；容器路径 cid:/path
        source, destination = command[2], command[3]

        def split_ref(value: str):
            if ":" in value and not value.startswith("/"):
                reference, _, path = value.partition(":")
                return reference, Path(path)
            return None, Path(value)

        src_ref, src_path = split_ref(source)
        dst_ref, dst_path = split_ref(destination)
        try:
            if src_ref is None and dst_ref is not None:
                info = self._by_reference(dst_ref)
                target = self.tmp / info["Id"] / dst_path.relative_to(dst_path.anchor)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(source, target)
            elif src_ref is not None and dst_ref is None:
                info = self._by_reference(src_ref)
                origin = self.tmp / info["Id"] / src_path.relative_to(src_path.anchor)
                if not origin.exists():
                    return self._fail("no such file")
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(origin, destination)
            return self._ok("")
        except OSError as exc:
            return self._fail(str(exc))

    def _by_reference(self, reference: str):
        if reference in self.containers:
            return self.containers[reference]
        for info in self.containers.values():
            if info["Id"] == reference:
                return info
        raise KeyError(reference)

    def _cmd_network(self, command: list[str]):
        sub = command[2]
        if sub == "inspect":
            reference = command[3] if len(command) > 3 else ""
            if reference in self.networks:
                return self._ok(json.dumps(self.networks[reference]))
            return self._fail(f"Error: No such network: {reference}")
        if sub == "create":
            name = command[-1]
            labels = {}
            if "--label" in command:
                for index, token in enumerate(command):
                    if token == "--label" and "=" in command[index + 1]:
                        key, value = command[index + 1].split("=", 1)
                        labels[key] = value
            self.networks[name] = {
                "Id": f"net{len(self.networks) + 1:04x}", "Name": name,
                "Labels": labels, "Options": {},
                "IPAM": {"Config": [{"Gateway": f"10.{len(self.networks)}.0.1"}]},
            }
            return self._ok(name + "\n")
        if sub == "rm":
            self.networks.pop(command[-1], None)
            return self._ok("")
        if sub == "disconnect":
            return self._ok("")
        if sub == "connect":
            name, container = command[-2], command[-1]
            info = self._by_reference(container)
            info["NetworkSettings"]["Networks"][name] = {"IPAddress": "10.99.0.7"}
            return self._ok("")
        return self._ok("")

    def _cmd_info(self, command: list[str]):
        if "--format" in command:
            return self._ok(json.dumps({"ServerVersion": "test", "Driver": "overlay2"}))
        return self._ok("")

    def _cmd_container(self, command: list[str]):
        if "inspect" in command:
            reference = command[-1]
            return self._ok("0\n" if reference in self.containers else "")
        return self._ok("")

    def _cmd_version(self, _command):
        return self._ok("Docker version 99-fake\n")

    def snapshot_scope_containers(self, scope: str) -> list[str]:
        return [
            name for name, info in self.containers.items()
            if info["Labels"].get("cybergym_e2e.scope") == scope
        ]
