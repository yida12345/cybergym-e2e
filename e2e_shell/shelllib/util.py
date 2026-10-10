"""外壳通用工具：进程身份、原子写、哈希、时间与目录工具。

所有模块共用的小函数集中在这里，避免在多个文件中复制粘贴。
时间戳使用 UTC ISO 格式，超时计算使用 monotonic；PID 身份包含
boot_id 与 /proc 启动时刻，避免系统重启或 PID 复用造成误判。
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO-8601 字符串（秒级）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def boot_id() -> str:
    """当前内核启动标识；用于 PID 身份核对，避免跨重启误判。"""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown-boot-id"


def pid_start_ticks(pid: int) -> str:
    """/proc/<pid>/stat 第 22 个字段（启动时钟滴答），读不到返回空串。"""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return ""
    # 进程名可能包含空格或括号，以最后一个 ')' 为界解析。
    end = text.rfind(")")
    if end < 0:
        return ""
    try:
        return text[end + 2:].split()[19]
    except IndexError:
        return ""


def pid_command(pid: int) -> str:
    """/proc/<pid>/cmdline（NUL 转空格），读不到返回空串。"""
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", "replace"
        )
    except OSError:
        return ""


def pid_alive(pid: int, expected_start: str | None = None) -> bool:
    """PID 是否存活；提供 expected_start 时同时核对启动时刻。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if expected_start is not None:
        return pid_start_ticks(pid) == expected_start and expected_start != ""
    return Path(f"/proc/{pid}").exists()


class FileLock:
    """基于 flock 的独占文件锁，用于 scope 级启动/导出互斥。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None

    def acquire(self, blocking: bool = True) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+")
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(self._handle.fileno(), flags)
        except OSError as exc:
            self._handle.close()
            self._handle = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
        return True

    def release(self) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


@contextlib.contextmanager
def file_lock(path: Path, blocking: bool = True) -> Iterator[bool]:
    lock = FileLock(path)
    acquired = lock.acquire(blocking=blocking)
    try:
        yield acquired
    finally:
        lock.release()


def fsync_dir(path: Path) -> None:
    """fsync 目录本身，保证 rename/create 持久化。"""
    fd = os.open(str(path), os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes, sync: bool = True) -> None:
    """临时文件 + fsync + 原子 rename 写入二进制内容。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            if sync:
                os.fsync(handle.fileno())
        os.replace(tmp_name, str(path))
        if sync:
            fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def atomic_write_json(path: Path, payload: Any, *, indent: int = 2, sync: bool = True) -> None:
    """JSON 序列化后的原子写入，ensure_ascii=False 保留中文。"""
    text = json.dumps(payload, ensure_ascii=False, indent=indent) + "\n"
    atomic_write_bytes(Path(path), text.encode("utf-8"), sync=sync)


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """文件内容 SHA-256；用于资产指纹与 receipt 校验。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def scope_hash(*parts: str) -> str:
    """由规范化字符串派生的短哈希，用于网络名、锁目录等 scope 标识。"""
    return sha256_text("\x1f".join(parts))[:12]


def dir_size_bytes(path: Path) -> int:
    """目录树总字节数；watchdog 检查 attempt 产物大小时使用。"""
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def safe_name_component(value: str, max_len: int = 48) -> str:
    """把任务路径等用户输入变成安全的文件/容器名成分。"""
    sanitized = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("._-")
    sanitized = sanitized.lower() or "unknown"
    return sanitized[:max_len].rstrip("-_.") or "unknown"


def hardlink_or_copy(source: Path, destination: Path, *, mode: str) -> Path:
    """按冻结策略复制资产：hardlink 仅同文件系统；copy 永远复制。

    hardlink 不是内容不可变保障：调用方必须保证源资产不变并在恢复前
    核对指纹。跨文件系统时 hardlink 会显式报错，除非 mode='copy'。
    """
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return destination
    if mode == "hardlink":
        try:
            os.link(source, destination)
            return destination
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise RuntimeError(
                    f"跨文件系统无法硬链接: {source} -> {destination}；"
                    "请设置 DATA_FREEZE_MODE=copy 或将输出放到同一文件系统"
                ) from exc
            raise
    if mode == "copy":
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
        return destination
    raise ValueError(f"未知冻结模式: {mode}")


def terminate_process_tree(pid: int, grace_seconds: float = 20.0) -> None:
    """向进程组发送 TERM，超时后 KILL；仅作用于确定存在的 PID。"""
    import signal

    if not pid_alive(pid):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
    deadline = time.monotonic() + grace_seconds
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    if pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


class TimeoutClock:
    """monotonic 预算钟：外层总预算与各阶段预算分别记录。"""

    def __init__(self, budget_seconds: float):
        self._start = time.monotonic()
        self._budget = float(budget_seconds)

    @property
    def start(self) -> float:
        return self._start

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def remaining(self) -> float:
        return self._budget - self.elapsed()

    def expired(self) -> bool:
        return self.remaining() <= 0
