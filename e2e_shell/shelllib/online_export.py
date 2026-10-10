"""在线增量导出（07v2）：finished attempt 的增量处理与原子发布。

支持 watch、不可变输入缓存、独立锁、有限失败重试、样本 ID 去重、
版本目录、原子发布、更新／撤回记录和提交中断恢复。在线结果注明
provisional，完成后进行全局核对。进程退出不停止 benchmark。

布局（与离线 07 分离，互不覆盖）::

    online/
    ├── successful.jsonl / failed.jsonl   # 全量视图（符号链接 → current）
    ├── sharegpt/   summary.json  manifest.json
    ├── vN/          # 增量目录（本次新增 + updates）
    └── .state/
        ├── run.lock
        ├── observations.sqlite   # 已处理 attempt 签名
        ├── objects/              # 不可变输入缓存
        ├── commits/  current -> commits/vN
        └── pending.json          # 仅提交/恢复期间存在
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from . import util
from .config import ShellConfig

ONLINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
  attempt_id TEXT PRIMARY KEY,
  signature TEXT NOT NULL,
  outcome TEXT,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL
);
"""


class OnlineExporter:
    def __init__(self, cfg: ShellConfig, *, online_root: Path | None = None):
        self.cfg = cfg
        self.root = Path(online_root) if online_root else cfg.run_scope / "online"
        self.state = self.root / ".state"
        self.objects = self.state / "objects"
        self.commits = self.state / "commits"
        self.observations_db = self.state / "observations.sqlite"

    # ---- 锁与初始化 ---------------------------------------------------

    @contextlib.contextmanager
    def _lock(self):
        self.state.mkdir(parents=True, exist_ok=True)
        lock = util.FileLock(self.state / "run.lock")
        if not lock.acquire(blocking=False):
            raise RuntimeError("另一个在线导出器正在运行（run.lock 已占用）")
        try:
            yield
        finally:
            lock.release()

    def _init_state(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects.mkdir(parents=True, exist_ok=True)
        self.commits.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(self.observations_db)) as db:
            db.executescript(ONLINE_SCHEMA)
            db.commit()

    # ---- 主流程 -------------------------------------------------------

    def run_once(self) -> dict[str, Any]:
        """处理一次增量；返回本次报告。失败（异常）不消耗版本号。"""
        with self._lock():
            self._init_state()
            self._recover_pending()
            observations = self._load_observations()
            finished = self._snapshot_finished()
            new_samples: list[dict[str, Any]] = []
            updates: list[dict[str, Any]] = []
            for attempt in finished:
                signature = attempt["signature"]
                previous = observations.get(attempt["attempt_id"])
                if previous and previous == signature:
                    continue
                frozen = self._freeze(attempt)
                if frozen is None:
                    continue
                if previous:
                    updates.append(
                        {"attempt_id": attempt["attempt_id"], "kind": "updated",
                         "previous": previous, "new": signature}
                    )
                else:
                    new_samples.append(
                        {"attempt_id": attempt["attempt_id"], "outcome": attempt["outcome"],
                         "signature": signature}
                    )
                observations[attempt["attempt_id"]] = signature
            report = self._publish(new_samples, updates, observations)
            self._save_observations(observations)
            return report

    def watch(self, interval_seconds: int, *, should_stop: Callable[[], bool] | None = None) -> None:
        """监控模式：每周期发布版本（含零增量）；信号/Ctrl-C 优雅退出。"""
        import signal

        stop = {"flag": False}

        def handler(*_):
            stop["flag"] = True

        with contextlib.suppress(ValueError):
            signal.signal(signal.SIGTERM, handler)
        while not stop["flag"] and not (should_stop and should_stop()):
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - 错误归档，下周期重试
                self._write_last_error(exc)
            if should_stop and should_stop():
                break
            time.sleep(max(5, interval_seconds))

    def _write_last_error(self, exc: Exception) -> None:
        util.atomic_write_json(self.state / "last_error.json", {
            "error": f"{type(exc).__name__}: {exc}", "at": util.utc_now_iso(),
        })

    # ---- 输入冻结与签名 -----------------------------------------------

    def _snapshot_finished(self) -> list[dict[str, Any]]:
        from .queue import connect

        rows = []
        with contextlib.closing(connect(self.cfg.queue_db_path(), readonly=True)) as db:
            for row in db.execute(
                "SELECT a.attempt_id,a.task_id,a.repeat_index,a.generation,a.outcome,"
                "a.output_dir,t.task_path FROM attempts a JOIN tasks t ON t.id=a.task_id "
                "WHERE a.state='finished' AND a.outcome IN ('success','failed') "
                "ORDER BY a.id"
            ):
                output_dir = Path(row["output_dir"])
                result_path = output_dir / "result.json"
                signature_parts = [str(row["attempt_id"]), row["outcome"]]
                if result_path.exists():
                    signature_parts.append(util.sha256_file(result_path))
                signature = util.scope_hash(*signature_parts)
                rows.append({
                    "attempt_id": row["attempt_id"], "task_id": row["task_id"],
                    "task_path": row["task_path"], "repeat_index": row["repeat_index"],
                    "generation": row["generation"], "outcome": row["outcome"],
                    "output_dir": str(output_dir), "signature": signature,
                })
        return rows

    def _freeze(self, attempt: dict[str, Any]) -> dict[str, Any] | None:
        """把 attempt 必要输入冻结到不可变缓存。"""
        output_dir = Path(attempt["output_dir"])
        if not output_dir.exists():
            return None
        target = self.objects / attempt["attempt_id"]
        target.mkdir(parents=True, exist_ok=True)
        for name in ("result.json",):
            source = output_dir / name
            if source.exists():
                util.hardlink_or_copy(source, target / name, mode="copy")
        sessions = output_dir / "cc_home" / "projects"
        if sessions.is_dir():
            for session in sessions.rglob("*.jsonl"):
                relative = session.relative_to(sessions)
                util.hardlink_or_copy(
                    session, target / "logs" / "projects" / relative, mode="copy"
                )
        console = output_dir / "logs" / "console.log"
        if console.exists():
            util.hardlink_or_copy(console, target / "console.log", mode="copy")
        return {"frozen": True}

    # ---- 发布 ---------------------------------------------------------

    def _publish(
        self,
        new_samples: list[dict[str, Any]],
        updates: list[dict[str, Any]],
        observations: dict[str, str],
    ) -> dict[str, Any]:
        """原子发布新版本目录并切换 current；提交中断由 pending.json 恢复。"""
        number = 1
        while (self.commits / f"v{number}").exists():
            number += 1
        version = self.commits / f"v{number}"
        version.mkdir(parents=True)

        pending = self.state / "pending.json"
        util.atomic_write_json(pending, {"version": version.name, "started": util.utc_now_iso()})
        try:
            # 全量视图（从已冻结 objects 重建；保证 current 完整）
            successful: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []
            for attempt_id, signature in sorted(observations.items()):
                frozen = self.objects / attempt_id
                result_path = frozen / "result.json"
                if not result_path.exists():
                    continue
                result = util.read_json(result_path)
                record = {
                    "attempt_id": attempt_id,
                    "signature": signature,
                    "checks": result.get("checks", []),
                    "result": result.get("result"),
                    "provisional": True,
                }
                if result.get("result") == "success":
                    successful.append(record)
                elif result.get("result") == "failed":
                    failed.append(record)
            _write_jsonl(version / "successful.jsonl", successful)
            _write_jsonl(version / "failed.jsonl", failed)
            _write_jsonl(version / "updates.jsonl", updates)
            _write_jsonl(version / "sample_metadata.jsonl", new_samples)
            manifest = {
                "version": version.name,
                "generated_at": util.utc_now_iso(),
                "scope": self.cfg.scope,
                "stage_policy": self.cfg.stage_policy.to_manifest(),
                "new_samples": len(new_samples),
                "updates": len(updates),
                "total_successful": len(successful),
                "total_failed": len(failed),
                "provisional": True,
            }
            util.atomic_write_json(version / "manifest.json", manifest)
            util.atomic_write_json(
                version / "summary.json",
                {"successful": len(successful), "failed": len(failed),
                 "new": len(new_samples), "updates": len(updates),
                 "provisional": True},
            )
            # 原子切换 current：临时符号链接 + os.replace
            current_link = self.state / "current"
            tmp_link = self.state / ".current.tmp"
            if current_link.is_symlink() or current_link.exists():
                current_link.unlink()
            os.symlink(str(version), str(tmp_link))
            os.replace(str(tmp_link), str(current_link))
            # 根级公开符号链接指向 current 内文件
            self._publish_root_links(version)
        finally:
            with contextlib.suppress(OSError):
                pending.unlink()

        # 保留最近两代私有全量视图；公开 vN 始终保留
        _prune_commits(self.commits, keep_private=2)
        report = {"version": version.name, "new_samples": len(new_samples),
                  "updates": len(updates), "total_successful": len(successful),
                  "total_failed": len(failed), "provisional": True}
        return report

    def _publish_root_links(self, version: Path) -> None:
        for name in ("successful.jsonl", "failed.jsonl", "summary.json", "manifest.json"):
            target = self.root / name
            with contextlib.suppress(OSError):
                if target.is_symlink() or target.exists():
                    target.unlink()
            os.symlink(str(version / name), str(target))

    # ---- observations 缓存 --------------------------------------------

    def _load_observations(self) -> dict[str, str]:
        if not self.observations_db.exists():
            return {}
        with contextlib.closing(sqlite3.connect(self.observations_db)) as db:
            return {
                row[0]: row[1] for row in db.execute(
                    "SELECT attempt_id, signature FROM observations"
                )
            }

    def _save_observations(self, observations: dict[str, str]) -> None:
        with contextlib.closing(sqlite3.connect(self.observations_db)) as db:
            db.executescript(ONLINE_SCHEMA)
            for attempt_id, signature in observations.items():
                now = time.time()
                db.execute(
                    "INSERT INTO observations(attempt_id,signature,first_seen,last_seen) "
                    "VALUES(?,?,?,?) ON CONFLICT(attempt_id) DO UPDATE SET "
                    "signature=excluded.signature,last_seen=excluded.last_seen",
                    (attempt_id, signature, now, now),
                )
            db.commit()

    def _recover_pending(self) -> None:
        """提交中断恢复：存在 pending 指向的未提交 vN 时删除它，下周期重建。"""
        pending = self.state / "pending.json"
        if not pending.exists():
            return
        try:
            payload = util.read_json(pending)
        except (OSError, ValueError):
            pending.unlink(missing_ok=True)
            return
        version = self.commits / payload.get("version", "")
        # 若该版本尚未成为 current（即发布未完成），删除以重建
        current_link = self.state / "current"
        current_target = ""
        if current_link.is_symlink():
            current_target = os.readlink(str(current_link))
        if str(version) != current_target:
            with contextlib.suppress(OSError):
                shutil.rmtree(version)
        pending.unlink(missing_ok=True)


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _prune_commits(commits: Path, *, keep_private: int) -> None:
    """保留最近 keep_private 代已发布视图之外多余的私有副本。"""
    versions = sorted(commits.glob("v*"), key=lambda p: int(p.name[1:]))
    published = []
    link = commits.parent / "current"
    if link.is_symlink():
        published = os.readlink(str(link))
    for version in versions[:-keep_private] if len(versions) > keep_private else []:
        if str(version) == published:
            continue
        with contextlib.suppress(OSError):
            shutil.rmtree(version)
