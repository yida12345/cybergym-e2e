"""镜像共享与生命周期：digest 锁、运行 lease、owned 账本与精确清理。

E2E 多个任务共享 build image，不能按 ARVO ID 整题独占锁（同镜像任务会被
串行化），按规范化 digest 使用短 pull/delete 互斥锁；运行期使用共享
lease，允许不同任务的容器并行使用同一镜像。

清理必须核对消费者、所有权和 image ID：本轮创建、无活动 lease、image ID
仍匹配才删除；操作前已有的镜像和未知外部引用保留。禁止全局 prune。
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import util
from .docker_control import DockerControl, docker_startup_slot

LEASE_SWEEP_INTERVAL = 60.0

def _digest_key(image: str) -> str:
    return hashlib.sha256(image.encode()).hexdigest()[:24]

@contextlib.contextmanager
def image_operation_lock(image: str, lock_root: Path):
    """按规范化镜像引用的短互斥锁：覆盖 pull 与 delete 操作。"""
    lock_root.mkdir(parents=True, exist_ok=True)
    with (lock_root / f"{_digest_key(image)}.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

@dataclass
class ImageLease:
    """一个活动消费者对镜像的运行租约。"""

    image: str
    scope: str
    launch_id: str
    attempt_id: str
    pid: int
    pid_start: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "image": self.image,
            "scope": self.scope,
            "launch_id": self.launch_id,
            "attempt_id": self.attempt_id,
            "pid": self.pid,
            "pid_start": self.pid_start,
            "created_at": util.utc_now_iso(),
        }

    def alive(self) -> bool:
        return self.pid_start != "" and util.pid_start_ticks(self.pid) == self.pid_start

class ImageManager:
    """镜像获取、lease 管理与清理。

    shared_state_root 是跨实验共享的 lease 元数据目录（默认
    output/shared_state）；ledger 是本轮 run 级 owned 账本，同 EXP resume
    不会遗忘上一 launch 的 owned 镜像。
    """

    def __init__(
        self,
        docker: DockerControl,
        shared_state_root: Path,
        ledger_path: Path,
        *,
        lock_root: Path | None = None,
        cleanup_enabled: bool = True,
    ):
        self.docker = docker
        self.shared_state_root = Path(shared_state_root)
        self.ledger_path = Path(ledger_path)
        self.lock_root = lock_root or Path("/tmp/cg-e2e-image-locks")
        self.cleanup_enabled = cleanup_enabled

    # ---- lease ---------------------------------------------------------

    def _lease_file(self, image: str) -> Path:
        directory = self.shared_state_root / "image_leases" / _digest_key(image)
        directory.mkdir(parents=True, exist_ok=True)
        return directory / "leases.jsonl"

    def acquire_lease(self, lease: ImageLease) -> None:
        """登记运行 lease；先清理死 PID 的陈旧 lease。"""
        path = self._lease_file(lease.image)
        active = self._sweep_leases(path)
        active.append(lease.to_dict())
        util.atomic_write_json(path, active)

    def release_lease(self, lease: ImageLease) -> None:
        path = self._lease_file(lease.image)
        active = self._sweep_leases(path)
        kept = [
            item
            for item in active
            if not (
                item.get("attempt_id") == lease.attempt_id
                and item.get("scope") == lease.scope
            )
        ]
        util.atomic_write_json(path, kept)

    def active_leases(self, image: str) -> list[dict[str, Any]]:
        return self._sweep_leases(self._lease_file(image))

    def _sweep_leases(self, path: Path) -> list[dict[str, Any]]:
        """读取 lease 文件并剔除 PID 已消失的陈旧条目（返回仍活动的列表）。"""
        if not path.exists():
            return []
        try:
            payload = util.read_json(path)
        except (OSError, ValueError):
            return []
        if not isinstance(payload, list):
            return []
        active: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            pid = item.get("pid")
            pid_start = item.get("pid_start")
            if isinstance(pid, int) and isinstance(pid_start, str):
                if pid_start and util.pid_start_ticks(pid) == pid_start:
                    active.append(item)
        return active

    # ---- 账本 ----------------------------------------------------------

    def _ledger_entries(self) -> list[dict[str, Any]]:
        if not self.ledger_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
        return entries

    def _append_ledger(self, entry: dict[str, Any]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            import os as _os

            _os.fsync(handle.fileno())

    # ---- 获取 ----------------------------------------------------------

    def ensure_image(self, image: str, *, scope: str, launch_id: str, attempt_id: str) -> dict[str, Any]:
        """按需获取镜像：已存在则记录复用，否则在锁内 pull。

        返回 {image_id, preexisting}；账本记录实际 image ID 与操作前
        是否已存在。共享镜像允许并行消费（pull 互斥，运行不加整题锁）。
        """
        with image_operation_lock(image, self.lock_root):
            preexisting = self.docker.image_exists(image)
            if preexisting:
                image_id = self.docker.image_id(image)
            else:
                image_id = self.docker.pull(image)
            entry = {
                "image": image,
                "image_id": image_id,
                "preexisting": preexisting,
                "scope": scope,
                "launch_id": launch_id,
                "attempt_id": attempt_id,
                "at": util.utc_now_iso(),
            }
            self._append_ledger(entry)
        self.acquire_lease(
            ImageLease(
                image=image, scope=scope, launch_id=launch_id,
                attempt_id=attempt_id, pid=0, pid_start="",
            )
        )
        # lease 的 PID 由调用方在 worker 进程内刷新（见 refresh_lease_pid）
        return {"image_id": image_id, "preexisting": preexisting}

    def refresh_lease_pid(self, image: str, scope: str, launch_id: str, attempt_id: str, pid: int) -> None:
        """把 lease 的 PID 换成实际 worker 进程；用于死锁检测。"""
        lease = ImageLease(
            image=image, scope=scope, launch_id=launch_id,
            attempt_id=attempt_id, pid=pid, pid_start=util.pid_start_ticks(pid),
        )
        path = self._lease_file(image)
        active = self._sweep_leases(path)
        active = [item for item in active if item.get("attempt_id") != attempt_id or item.get("scope") != scope]
        active.append(lease.to_dict())
        util.atomic_write_json(path, active)

    # ---- 清理 ----------------------------------------------------------

    def cleanup_owned_images(
        self,
        *,
        scope: str,
        consumer_tasks_finished: dict[str, bool],
        task_image_map: dict[str, str],
    ) -> dict[str, Any]:
        """清理本轮 owned 镜像。

        条件：账本中记录为“本轮创建”（preexisting=False）、该镜像的全部
        消费者任务已完成、无活动 lease、当前 image ID 仍匹配。操作前已有
        镜像与未知外部引用保留；失败单列可重试；禁止全局 prune。
        """
        report: dict[str, Any] = {"removed": [], "preserved": [], "failed": [], "skipped": []}
        if not self.cleanup_enabled:
            report["skipped"].append("cleanup_disabled")
            return report

        owned: dict[str, dict[str, Any]] = {}
        for entry in self._ledger_entries():
            if entry.get("scope") != scope or entry.get("preexisting"):
                continue
            owned[entry["image"]] = entry

        consumers: dict[str, list[str]] = {}
        for task, image in task_image_map.items():
            consumers.setdefault(image, []).append(task)

        for image, entry in owned.items():
            task_list = consumers.get(image, [])
            if not task_list:
                continue
            if not all(consumer_tasks_finished.get(task, False) for task in task_list):
                report["skipped"].append({"image": image, "reason": "consumers_running"})
                continue
            if self.active_leases(image):
                report["skipped"].append({"image": image, "reason": "active_lease"})
                continue
            with image_operation_lock(image, self.lock_root):
                current_id = self.docker.image_id(image)
                if current_id is None:
                    report["removed"].append({"image": image, "reason": "already_absent"})
                    continue
                if current_id != entry.get("image_id"):
                    report["preserved"].append(
                        {"image": image, "reason": "image_id_changed", "image_id": current_id}
                    )
                    continue
                try:
                    with docker_startup_slot(
                        self.docker.startup_lock_dir, self.docker.startup_concurrency
                    ):
                        result = self.docker.runner(["docker", "image", "rm", image], 300)
                    if result.returncode == 0:
                        report["removed"].append({"image": image, "image_id": current_id})
                    else:
                        report["failed"].append(
                            {"image": image, "error": (result.stderr or result.stdout).strip()[-300:]}
                        )
                except Exception as exc:  # noqa: BLE001 - 单镜像失败不影响其余
                    report["failed"].append({"image": image, "error": str(exc)})
        return report

def load_task_image_map(manifest: dict[str, Any]) -> dict[str, str]:
    """manifest 任务 -> ACR 镜像引用映射。"""
    return {
        task["task_path"]: task["build_image_acr"] for task in manifest.get("tasks", [])
    }
