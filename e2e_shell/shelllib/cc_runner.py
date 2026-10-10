"""Claude Code 执行器：环境、流式日志、会话目录与终态后 idle guard。

与官方 E2E 差异（均为外壳职责）：
- 持续落盘 console 输出（官方在进程结束后一次性写 attempt 日志）；
- 显式透传 ANTHROPIC_BASE_URL 指向本 scope capture（经 Squid）；
- 统一所有模型别名，避免小模型请求走其他地址；
- 独立、持久化的会话目录（CLAUDE_CONFIG_DIR 按 attempt 挂载）；
- 共享 CC runtime 只读挂载，按容器实际 agent UID 设置会话目录权限。

REASONING_EFFORT（默认 high）通过 CLAUDE_CODE_EFFORT_LEVEL 传给 CLI；
实际请求字段与上游支持情况的证据由 04 的辅助请求 + capture 请求体
检查提供（check_effort_evidence），不能只设置字符串就声称已生效。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import util
from .docker_control import DockerControl

OFFICIAL_DISALLOWED_TOOLS = "WebFetch,WebSearch,Task,MCPSearch,NotebookEdit,Skill,AskUserQuestion"


class ResultIdleGuard:
    """终态 result 事件后的静默保护。

    只在观察到 CC stream-json 的 {"type":"result"} 事件后启动 idle 计时，
    因此不会把“正常等待模型响应”误判为静默异常。
    """

    def __init__(self, idle_seconds: float, check_interval: float = 2.0):
        self.idle_seconds = idle_seconds
        self.check_interval = check_interval
        self._lock = threading.Lock()
        self._last_output = time.monotonic()
        self._result_seen = False
        self._pending = ""
        self._reason: str | None = None
        self._stop = threading.Event()

    def feed(self, chunk: str) -> None:
        if not chunk:
            return
        with self._lock:
            self._last_output = time.monotonic()
            self._pending += chunk
            while "\n" in self._pending:
                line, self._pending = self._pending.split("\n", 1)
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(event, dict) and event.get("type") == "result":
                    self._result_seen = True

    @property
    def reason(self) -> str | None:
        return self._reason

    def _evaluate(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                result_seen = self._result_seen
                idle = time.monotonic() - self._last_output
            if result_seen and idle >= self.idle_seconds:
                self._reason = f"result_idle_timeout seconds={self.idle_seconds}"
                return
            self._stop.wait(self.check_interval)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._evaluate, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def __enter__(self) -> "ResultIdleGuard":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()


@dataclass
class CcRunResult:
    exit_code: int | None
    stop_reason: str | None
    duration_seconds: float
    result_event_seen: bool = False


@dataclass
class CcEnvironment:
    """CC 执行所需的环境与路径。"""

    base_url: str  # capture 地址（含路由前缀）
    api_key: str
    model: str
    reasoning_effort: str
    api_timeout_ms: int
    api_max_retries: int
    max_output_tokens: int
    result_idle_timeout: int
    force_idle_timeout: int
    squid_url: str | None  # 运行网络代理；None 表示未启用网络隔离
    runtime_dir_in_container: str = "/tools"

    def env_vars(self) -> dict[str, str]:
        """容器内 CC 环境变量；所有默认模型别名统一为配置 model。"""
        env = {
            "ANTHROPIC_BASE_URL": self.base_url,
            "ANTHROPIC_API_KEY": self.api_key,
            "ANTHROPIC_MODEL": self.model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": self.model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": self.model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": self.model,
            "CLAUDE_CODE_SUBAGENT_MODEL": self.model,
            "CLAUDE_CODE_EFFORT_LEVEL": self.reasoning_effort,
            "API_TIMEOUT_MS": str(self.api_timeout_ms),
            "CLAUDE_CODE_MAX_RETRIES": str(self.api_max_retries),
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(self.max_output_tokens),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "IS_SANDBOX": "1",
            "CLAUDE_CONFIG_DIR": "/cc-home",
        }
        if self.squid_url:
            env.update(
                {
                    "HTTP_PROXY": self.squid_url,
                    "HTTPS_PROXY": self.squid_url,
                    "http_proxy": self.squid_url,
                    "https_proxy": self.squid_url,
                    "NO_PROXY": "localhost,127.0.0.1",
                    "no_proxy": "localhost,127.0.0.1",
                }
            )
        return env


class CcRunner:
    """在 agent 容器内运行 Claude Code。"""

    def __init__(self, docker: DockerControl):
        self.docker = docker

    def install_shared_runtime(
        self, container: str, env: CcEnvironment, *, node_bin: str
    ) -> dict[str, str]:
        """共享 runtime 模式：创建 agent 用户并验证二进制兼容性。

        先检查 Node 二进制与镜像架构、动态库兼容性——不假设宿主打包的
        工具在全部镜像内都可执行。返回记录（版本信息）。
        """
        setup = """
set -e
useradd -m -s /bin/bash agent 2>/dev/null || true
echo 'agent ALL=(ALL) NOPASSWD: ALL' >> /etc/sudoers
chmod a+rx /root
chown -R agent:agent /src /src_backup /output /logs 2>/dev/null || true
"""
        code, out, err = self.docker.exec_run(container, setup, timeout=300)
        if code != 0:
            raise RuntimeError(f"agent 用户准备失败: {err[-300:]}")
        check = f"{node_bin} --version && {node_bin} -e 'console.log(1)'"
        code, out, err = self.docker.exec_run(container, check, timeout=120)
        if code != 0:
            raise RuntimeError(
                f"Node 二进制在该镜像内不可执行（架构/动态库不兼容）: {err[-300:]}"
            )
        return {"node_version": out.strip(), "node_bin": node_bin}

    def agent_uid(self, container: str) -> tuple[int, int]:
        """容器内 agent 用户的 UID/GID；会话目录按此设置权限。"""
        code, out, _ = self.docker.exec_run(container, "id -u agent && id -g agent", timeout=60)
        if code != 0:
            return 1000, 1000
        parts = out.split()
        try:
            return int(parts[0]), int(parts[1]) if len(parts) > 1 else int(parts[0])
        except (ValueError, IndexError):
            return 1000, 1000

    def claude_command(self, env: CcEnvironment) -> str:
        """官方 E2E 的 CC 调用方式；runtime 以只读挂载提供。"""
        claude_bin = f"{env.runtime_dir_in_container}/bin/claude"
        return (
            f"su agent -c 'cd /src && {claude_bin} -p \"$(cat /src/.prompt.txt)\" "
            f"--disallowedTools \"{OFFICIAL_DISALLOWED_TOOLS}\" "
            f"--output-format stream-json --verbose --dangerously-skip-permissions'"
        )

    def run(
        self,
        container: str,
        prompt: str,
        console_log_path: Path,
        *,
        env: CcEnvironment,
        timeout_seconds: float,
        stop_check: Callable[[], str | None] | None = None,
        log_sink: Callable[[str], None] | None = None,
    ) -> CcRunResult:
        """执行 CC：流式输出持续写 host console，同时喂给 idle guard。"""
        # 提示写入容器内文件（官方方式；避免命令行长度与转义问题）。
        heredoc = "cat > /src/.prompt.txt << 'PROMPT_EOF'\n" + prompt + "\nPROMPT_EOF"
        code, _, err = self.docker.exec_run(container, heredoc, timeout=60)
        if code != 0:
            raise RuntimeError(f"写入提示失败: {err[-300:]}")

        guard = ResultIdleGuard(env.result_idle_timeout)
        started = time.monotonic()
        command = self.claude_command(env)
        env_vars = env.env_vars()

        console_log_path = Path(console_log_path)
        console_log_path.parent.mkdir(parents=True, exist_ok=True)

        process = self.docker.exec_stream_lines(
            container, command, env=env_vars, workdir=None
        )
        guard.start()
        reason: str | None = None
        result_seen = False
        while True:
            chunk = process.stdout.readline()
            if chunk:
                with console_log_path.open("ab") as handle:
                    handle.write(chunk)
                    handle.flush()
                text = chunk.decode("utf-8", "replace")
                result_seen = result_seen or _has_result_event(text)
                guard.feed(text)
                if log_sink:
                    log_sink(text)
            if process.poll() is not None:
                break
            if time.monotonic() - started > timeout_seconds:
                reason = "agent_timeout"
                break
            if guard.reason:
                reason = guard.reason
                break
            if stop_check is not None:
                stop_reason = stop_check()
                if stop_reason:
                    reason = stop_reason
                    break
        if reason:
            util.terminate_process_tree(process.pid, grace_seconds=10.0)
        exit_code = process.wait()
        guard.stop()
        return CcRunResult(
            exit_code=exit_code,
            stop_reason=reason,
            duration_seconds=round(time.monotonic() - started, 3),
            result_event_seen=result_seen,
        )


def _has_result_event(text: str) -> bool:
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return True
    return False


def check_effort_evidence(capture_raw_dir: Path, model: str) -> dict:
    """从 capture 请求体核对 reasoning effort 的实际字段与上游支持情况。

    只设置字符串不能证明生效：读取最近一次 messages 请求的 request.json
    body，检查 thinking/reasoning 相关字段是否存在及其值；配合 04 的
    辅助请求使用，与正式任务的请求区分。
    """
    candidates: list[Path] = []
    completed = Path(capture_raw_dir) / "completed"
    if completed.is_dir():
        candidates.extend(sorted(completed.glob("*/request.json")))
    evidence: dict = {"checked_requests": len(candidates), "model": model,
                      "effort_fields": [], "supported": None}
    for request_path in reversed(candidates[-20:]):
        try:
            payload = util.read_json(request_path)
        except (OSError, ValueError):
            continue
        body = payload.get("body") or {}
        content = body.get("content") if isinstance(body, dict) else None
        _ = content
        fields: dict[str, object] = {}
        if isinstance(body, dict):
            for key in ("thinking", "reasoning", "reasoning_effort", "thinking_budget"):
                if key in body:
                    fields[key] = body[key]
            if body.get("model") != model:
                continue
        if fields:
            evidence["effort_fields"].append(fields)
            evidence["supported"] = True
            break
    if not evidence["effort_fields"]:
        evidence["supported"] = False if candidates else None
    return evidence
