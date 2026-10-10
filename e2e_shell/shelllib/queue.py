"""SQLite WAL 任务池：短事务领取、repeat/generation、崩溃窗口恢复。

领取单位是 (task, mode)；一个 worker 领取后串行完成 N 个独立 repeat。
逻辑 repeat 与实际 attempt 分开记录：repeat_slots 表达逻辑完成度，
attempts 记录每次实际执行。恢复不能拿“已完成数量 + 1”猜下一 repeat，
必须逐 slot 核对状态与有效 finished_attempt_id。

关键约束：
- 对 starting/running 状态的逻辑 repeat 建部分 UNIQUE 索引，数据库层面
  保证同一个逻辑 repeat 只有一个当前有效执行者；
- 结果先落盘并写 receipt，再提交完成状态；恢复时先验证 receipt 才
  补记完成，避免重复调用模型；
- WAL 只在初始化时设定；领取和状态更新使用短 BEGIN IMMEDIATE 事务、
  有限锁冲突重试；06 状态查询使用 query-only 连接。
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import util

SCHEMA_VERSION = 1

TASK_STATES = ("pending", "running", "done")
SLOT_STATES = ("pending", "running", "done")
ATTEMPT_STATES = ("starting", "running", "finished", "abandoned")

LOCK_RETRIES = 6
LOCK_BASE_DELAY = 0.05


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """标准连接；WAL 模式只在初始化时设定，这里不动 journal_mode。"""
    if readonly:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA query_only=ON")
        return db
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    return db


def write_transaction(
    db: sqlite3.Connection, operation: Callable[[sqlite3.Connection], Any], retries: int = LOCK_RETRIES
) -> Any:
    """短序列化写事务；SQLite 锁冲突时有限重试，不把整个池打死。"""
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(retries + 1):
        try:
            db.execute("BEGIN IMMEDIATE")
            result = operation(db)
            db.commit()
            return result
        except sqlite3.OperationalError as exc:
            db.rollback()
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            last_error = exc
            if attempt < retries:
                time.sleep(min(LOCK_BASE_DELAY * (2**attempt), 1.0))
        except BaseException:
            db.rollback()
            raise
    raise last_error if last_error else RuntimeError("write_transaction failed")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  task_path TEXT NOT NULL,
  mode TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  owner INTEGER,
  generation INTEGER NOT NULL DEFAULT 0,
  started REAL,
  finished REAL,
  error TEXT,
  UNIQUE(task_path, mode)
);
CREATE TABLE IF NOT EXISTS repeat_slots (
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  repeat_index INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',
  generation INTEGER NOT NULL DEFAULT 0,
  finished_attempt_id TEXT,
  finished_at REAL,
  UNIQUE(task_id, repeat_index)
);
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id TEXT NOT NULL UNIQUE,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  repeat_index INTEGER NOT NULL,
  generation INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'starting',
  phase TEXT,
  launch_id TEXT NOT NULL,
  worker_rank INTEGER,
  pid INTEGER,
  pid_start TEXT,
  started REAL NOT NULL,
  finished REAL,
  outcome TEXT,
  error TEXT,
  output_dir TEXT NOT NULL,
  receipt_hash TEXT,
  FOREIGN KEY(task_id) REFERENCES tasks(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_active
  ON attempts(task_id, repeat_index)
  WHERE state IN ('starting','running');
CREATE TABLE IF NOT EXISTS stage_results (
  attempt_id TEXT NOT NULL,
  stage INTEGER NOT NULL,
  raw_status TEXT,
  normalized_status TEXT,
  reason TEXT,
  duration_seconds REAL,
  exit_code INTEGER,
  evidence_path TEXT,
  UNIQUE(attempt_id, stage)
);
CREATE TABLE IF NOT EXISTS workers (
  rank INTEGER PRIMARY KEY,
  pid INTEGER,
  pid_start TEXT,
  state TEXT,
  current_task INTEGER,
  current_repeat INTEGER NOT NULL DEFAULT 0,
  current_phase TEXT,
  started REAL,
  heartbeat REAL,
  tasks_completed INTEGER NOT NULL DEFAULT 0,
  attempts_completed INTEGER NOT NULL DEFAULT 0,
  restarts INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE TABLE IF NOT EXISTS launches (
  id TEXT PRIMARY KEY,
  started REAL NOT NULL,
  finished REAL,
  supervisor_pid INTEGER,
  supervisor_pid_start TEXT,
  boot_id TEXT NOT NULL,
  workers INTEGER NOT NULL,
  capture_dir TEXT,
  state TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS image_cleanup (
  image TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  updated REAL NOT NULL,
  image_id TEXT,
  error TEXT
);
"""


class TaskPool:
    """任务池操作封装；supervisor/worker/状态查询共用同一 schema。"""

    def __init__(self, db_path: Path):
        self.path = Path(db_path)

    # ---- 初始化 -------------------------------------------------------

    def initialize(self, tasks: Sequence[str], mode: str, repeat_n: int, workers: int) -> None:
        """初始化队列；只允许在空库上执行，防止重复播种。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(connect(self.path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA_SQL)

            def seed(connection: sqlite3.Connection) -> None:
                existing = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                if existing:
                    raise RuntimeError(
                        "queue already initialized; resume with the same EXP/ROUND_NUMBER"
                    )
                for ordinal, task in enumerate(tasks, 1):
                    cursor = connection.execute(
                        "INSERT INTO tasks(task_path,mode,ordinal,state) VALUES(?,?,?,'pending')",
                        (task, mode, ordinal),
                    )
                    task_id = cursor.lastrowid
                    connection.executemany(
                        "INSERT INTO repeat_slots(task_id,repeat_index,state) VALUES(?,?, 'pending')",
                        [(task_id, repeat) for repeat in range(1, repeat_n + 1)],
                    )
                connection.executemany(
                    "INSERT INTO workers(rank,state,heartbeat) VALUES(?,'starting',?)",
                    [(rank, time.time()) for rank in range(workers)],
                )
                connection.executemany(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                    [
                        ("schema_version", str(SCHEMA_VERSION)),
                        ("mode", mode),
                        ("repeat_count", str(repeat_n)),
                        ("worker_count", str(workers)),
                        ("stop_requested", "0"),
                        ("controller_state", "initialized"),
                        ("created_at", str(time.time())),
                    ],
                )

            write_transaction(db, seed)

    # ---- 基础查询 -----------------------------------------------------

    def meta_get(self, db: sqlite3.Connection, key: str) -> str | None:
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def stop_requested(self, db: sqlite3.Connection) -> bool:
        return (self.meta_get(db, "stop_requested") or "0") == "1"

    def controller_state(self) -> str | None:
        with contextlib.closing(connect(self.path, readonly=True)) as db:
            return self.meta_get(db, "controller_state")

    def counts(self) -> dict[str, Any]:
        with contextlib.closing(connect(self.path, readonly=True)) as db:
            return self._counts(db)

    def _counts(self, db: sqlite3.Connection) -> dict[str, Any]:
        tasks = {
            row["state"]: row["n"]
            for row in db.execute("SELECT state,COUNT(*) n FROM tasks GROUP BY state")
        }
        slots = {
            row["state"]: row["n"]
            for row in db.execute("SELECT state,COUNT(*) n FROM repeat_slots GROUP BY state")
        }
        outcomes = {
            row["outcome"]: row["n"]
            for row in db.execute(
                "SELECT outcome,COUNT(*) n FROM attempts WHERE state='finished' "
                "AND outcome IS NOT NULL GROUP BY outcome"
            )
        }
        total_slots = db.execute("SELECT COUNT(*) FROM repeat_slots").fetchone()[0]
        done_slots = db.execute(
            "SELECT COUNT(*) FROM repeat_slots WHERE state='done'"
        ).fetchone()[0]
        interrupted = db.execute(
            "SELECT COUNT(*) FROM attempts WHERE state='abandoned'"
        ).fetchone()[0]
        return {
            "tasks": tasks,
            "slots": slots,
            "outcomes": outcomes,
            "total_tasks": db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "planned_repeats": total_slots,
            "finished_repeats": done_slots,
            "abandoned_attempts": interrupted,
            "meta": {row["key"]: row["value"] for row in db.execute("SELECT * FROM meta")},
        }

    # ---- 领取与完成 ---------------------------------------------------

    def claim_task(self, rank: int) -> sqlite3.Row | str | None:
        """领取一个 (task, mode)；返回行、'WAIT' 或 None（停止/完成）。"""
        with contextlib.closing(connect(self.path)) as db:
            def operation(connection: sqlite3.Connection) -> tuple[str, int | None]:
                if self.stop_requested(connection):
                    return "STOP", None
                row = connection.execute(
                    "SELECT * FROM tasks WHERE state='pending' ORDER BY ordinal LIMIT 1"
                ).fetchone()
                if row is None:
                    unfinished = connection.execute(
                        "SELECT COUNT(*) FROM tasks WHERE state!='done'"
                    ).fetchone()[0]
                    if unfinished:
                        connection.execute(
                            "UPDATE workers SET state='waiting',current_task=NULL WHERE rank=?",
                            (rank,),
                        )
                        return "WAIT", None
                    connection.execute(
                        "UPDATE workers SET state='finished',current_task=NULL WHERE rank=?",
                        (rank,),
                    )
                    return "DONE", None
                connection.execute(
                    "UPDATE tasks SET state='running',owner=?,generation=generation+1,"
                    "started=COALESCE(started,?),finished=NULL,error=NULL WHERE id=?",
                    (rank, time.time(), row["id"]),
                )
                connection.execute(
                    "UPDATE workers SET state='running',current_task=?,started=? WHERE rank=?",
                    (row["id"], time.time(), rank),
                )
                return "CLAIMED", int(row["id"])

            state, task_id = write_transaction(db, operation)
            if state == "WAIT":
                return "WAIT"
            if state != "CLAIMED":
                return None
            return db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def next_pending_slot(self, task_id: int) -> sqlite3.Row | None:
        """下一个未完成 repeat；不用完成数量推断，逐 slot 查询状态。"""
        with contextlib.closing(connect(self.path, readonly=True)) as db:
            return db.execute(
                "SELECT * FROM repeat_slots WHERE task_id=? AND state='pending' "
                "ORDER BY repeat_index LIMIT 1",
                (task_id,),
            ).fetchone()

    def begin_attempt(
        self,
        task: sqlite3.Row,
        repeat_index: int,
        rank: int,
        launch_id: str,
        output_dir: Path,
    ) -> sqlite3.Row:
        """开始一次实际尝试：generation + 1，attempt 记录唯一 ID。

        部分唯一索引保证同一逻辑 repeat 只有一个活动 attempt；若已有
        活动执行者，事务失败并报错（调用方不应重试同一 slot）。
        """
        now = time.time()

        def operation(connection: sqlite3.Connection) -> int:
            slot = connection.execute(
                "SELECT * FROM repeat_slots WHERE task_id=? AND repeat_index=?",
                (task["id"], repeat_index),
            ).fetchone()
            if slot is None:
                raise RuntimeError(f"slot 不存在: task={task['id']} repeat={repeat_index}")
            if slot["state"] == "done":
                raise RuntimeError("slot 已完成，不能重复执行")
            generation = int(slot["generation"]) + 1
            attempt_id = (
                f"t{task['id']:06d}-{task['mode'].replace('-', '')}-"
                f"r{repeat_index:03d}-g{generation:03d}-{util.scope_hash(str(task['id']), task['mode'], str(repeat_index), str(generation))[:8]}"
            )
            connection.execute(
                "UPDATE repeat_slots SET state='running',generation=? WHERE id=?",
                (generation, slot["id"]),
            )
            cursor = connection.execute(
                "INSERT INTO attempts(attempt_id,task_id,repeat_index,generation,state,"
                "phase,launch_id,worker_rank,started,output_dir) "
                "VALUES(?,?,?,?,'starting','prepare',?,?,?,?)",
                (attempt_id, task["id"], repeat_index, generation, launch_id, rank, now, str(output_dir)),
            )
            connection.execute(
                "UPDATE workers SET current_repeat=?,current_phase='prepare' WHERE rank=?",
                (repeat_index, rank),
            )
            return int(cursor.lastrowid)

        with contextlib.closing(connect(self.path)) as db:
            row_id = write_transaction(db, operation)
            return db.execute("SELECT * FROM attempts WHERE id=?", (row_id,)).fetchone()

    def mark_attempt_running(
        self, attempt: sqlite3.Row, rank: int, pid: int
    ) -> None:
        with contextlib.closing(connect(self.path)) as db:
            def operation(connection: sqlite3.Connection) -> None:
                changed = connection.execute(
                    "UPDATE attempts SET state='running',pid=?,pid_start=? "
                    "WHERE id=? AND state='starting'",
                    (pid, util.pid_start_ticks(pid), attempt["id"]),
                ).rowcount
                if changed != 1:
                    raise RuntimeError("attempt state changed before process start")

            write_transaction(db, operation)

    def update_attempt_phase(self, attempt_id: str, phase: str, rank: int | None = None) -> None:
        with contextlib.closing(connect(self.path)) as db:
            def operation(connection: sqlite3.Connection) -> None:
                connection.execute(
                    "UPDATE attempts SET phase=? WHERE attempt_id=? AND state IN "
                    "('starting','running')",
                    (phase, attempt_id),
                )
                if rank is not None:
                    connection.execute(
                        "UPDATE workers SET current_phase=? WHERE rank=?", (phase, rank)
                    )

            write_transaction(db, operation)

    def store_stage_results(self, attempt_id: str, stage_payloads: Iterable[dict[str, Any]]) -> None:
        with contextlib.closing(connect(self.path)) as db:
            def operation(connection: sqlite3.Connection) -> None:
                for item in stage_payloads:
                    connection.execute(
                        "INSERT INTO stage_results(attempt_id,stage,raw_status,normalized_status,"
                        "reason,duration_seconds,exit_code,evidence_path) VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(attempt_id,stage) DO UPDATE SET "
                        "raw_status=excluded.raw_status,normalized_status=excluded.normalized_status,"
                        "reason=excluded.reason,duration_seconds=excluded.duration_seconds,"
                        "exit_code=excluded.exit_code,evidence_path=excluded.evidence_path",
                        (
                            attempt_id,
                            item["stage"],
                            item.get("raw_status"),
                            item.get("normalized_status"),
                            item.get("reason"),
                            item.get("duration_seconds"),
                            item.get("exit_code"),
                            item.get("evidence_path"),
                        ),
                    )

            write_transaction(db, operation)

    def finish_attempt(
        self,
        attempt: sqlite3.Row,
        rank: int,
        outcome: str,
        error: str | None,
        receipt_hash: str | None = None,
        count_repeat: bool = True,
    ) -> None:
        """提交 attempt 终态；count_repeat=True 时同时完成逻辑 repeat。"""
        now = time.time()

        def operation(connection: sqlite3.Connection) -> None:
            changed = connection.execute(
                "UPDATE attempts SET state=?,finished=?,outcome=?,error=?,receipt_hash=?,pid=NULL "
                "WHERE id=? AND state IN ('starting','running')",
                (
                    "finished" if count_repeat else "abandoned",
                    now, outcome, error, receipt_hash, attempt["id"],
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError("attempt state changed before completion")
            if count_repeat:
                connection.execute(
                    "UPDATE repeat_slots SET state='done',finished_attempt_id=?,finished_at=? "
                    "WHERE task_id=? AND repeat_index=? AND state='running'",
                    (attempt["attempt_id"], now, attempt["task_id"], attempt["repeat_index"]),
                )
                connection.execute(
                    "UPDATE workers SET attempts_completed=attempts_completed+1 WHERE rank=?",
                    (rank,),
                )

        with contextlib.closing(connect(self.path)) as db:
            write_transaction(db, operation)

    def finish_task_if_complete(self, task_id: int, rank: int) -> bool:
        """所有 slot 完成时把任务置 done；返回是否完成。"""
        with contextlib.closing(connect(self.path)) as db:
            def operation(connection: sqlite3.Connection) -> bool:
                pending = connection.execute(
                    "SELECT COUNT(*) FROM repeat_slots WHERE task_id=? AND state!='done'",
                    (task_id,),
                ).fetchone()[0]
                if pending:
                    return False
                connection.execute(
                    "UPDATE tasks SET state='done',finished=?,owner=NULL,error=NULL "
                    "WHERE id=? AND owner=? AND state='running'",
                    (time.time(), task_id, rank),
                )
                connection.execute(
                    "UPDATE workers SET state='idle',current_task=NULL,current_repeat=0,"
                    "current_phase=NULL,tasks_completed=tasks_completed+1 WHERE rank=?",
                    (rank,),
                )
                return True

            return write_transaction(db, operation)

    # ---- 停止与恢复 ---------------------------------------------------

    def request_stop(self) -> None:
        with contextlib.closing(connect(self.path)) as db:
            write_transaction(
                db,
                lambda c: c.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES('stop_requested','1')"
                ),
            )

    def clear_stop(self) -> None:
        with contextlib.closing(connect(self.path)) as db:
            def operation(connection: sqlite3.Connection) -> None:
                connection.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES('stop_requested','0')"
                )
                connection.execute(
                    "UPDATE meta SET value='resuming' WHERE key='controller_state'"
                )

            write_transaction(db, operation)

    def active_attempts(self) -> list[sqlite3.Row]:
        with contextlib.closing(connect(self.path, readonly=True)) as db:
            return list(
                db.execute(
                    "SELECT a.*,t.task_path,t.mode FROM attempts a "
                    "JOIN tasks t ON t.id=a.task_id "
                    "WHERE a.state IN ('starting','running')"
                )
            )

    def active_attempts_for_worker(self, rank: int) -> list[sqlite3.Row]:
        with contextlib.closing(connect(self.path, readonly=True)) as db:
            return list(
                db.execute(
                    "SELECT a.*,t.task_path,t.mode FROM attempts a "
                    "JOIN tasks t ON t.id=a.task_id "
                    "WHERE a.state IN ('starting','running') AND a.worker_rank=?",
                    (rank,),
                )
            )

    def update_worker_state_bulk(self, state: str) -> None:
        with contextlib.closing(connect(self.path)) as db:
            write_transaction(
                db,
                lambda c: c.execute(
                    "UPDATE workers SET state=?,current_task=NULL,current_repeat=0,"
                    "current_phase=NULL WHERE state NOT IN ('error','finished')",
                    (state,),
                ),
            )

    def recover_dead_attempt(
        self, attempt: sqlite3.Row, message: str, *, receipt_valid: bool = False
    ) -> str:
        """回收异常 worker 的 attempt。

        - receipt_valid=True：结果已落盘且验证通过，补记完成，不再调用模型；
        - 否则 abandon：逻辑 repeat 不计数，slot 回 pending，下个 worker
          以新 generation 重新执行；旧日志/结果/中断记录保留。
        返回 'finished' 或 'abandoned'。
        """
        now = time.time()

        def operation(connection: sqlite3.Connection) -> str:
            if receipt_valid:
                connection.execute(
                    "UPDATE attempts SET state='finished',finished=?,outcome='success',"
                    "error='recovered_from_receipt',pid=NULL WHERE id=? AND "
                    "state IN ('starting','running')",
                    (now, attempt["id"]),
                )
                connection.execute(
                    "UPDATE repeat_slots SET state='done',finished_attempt_id=?,finished_at=? "
                    "WHERE task_id=? AND repeat_index=? AND state='running'",
                    (attempt["attempt_id"], now, attempt["task_id"], attempt["repeat_index"]),
                )
                return "finished"
            connection.execute(
                "UPDATE attempts SET state='abandoned',finished=?,outcome='interrupted',"
                "error=?,pid=NULL WHERE id=? AND state IN ('starting','running')",
                (now, message, attempt["id"]),
            )
            connection.execute(
                "UPDATE repeat_slots SET state='pending' WHERE task_id=? AND repeat_index=? "
                "AND state='running'",
                (attempt["task_id"], attempt["repeat_index"]),
            )
            return "abandoned"

        with contextlib.closing(connect(self.path)) as db:
            return write_transaction(db, operation)

    def recover_interrupted_pool(self) -> dict[str, int]:
        """resume 时的整体恢复：stop 残留与 stale 状态归位。

        由 scope 启动锁保护；恢复中再次停止不会让两个 worker 领取同一
        repeat（活动 attempt 的 slot 保持 running，直到被显式回收）。
        """
        stats = {"attempts_abandoned": 0, "slots_reset": 0, "tasks_reset": 0}

        def operation(connection: sqlite3.Connection) -> None:
            rows = list(
                connection.execute(
                    "SELECT a.*,t.task_path FROM attempts a JOIN tasks t ON t.id=a.task_id "
                    "WHERE a.state IN ('starting','running')"
                )
            )
            for row in rows:
                connection.execute(
                    "UPDATE attempts SET state='abandoned',finished=?,"
                    "outcome='interrupted',error='recovered_at_launch',pid=NULL WHERE id=?",
                    (time.time(), row["id"]),
                )
                connection.execute(
                    "UPDATE repeat_slots SET state='pending' WHERE task_id=? AND repeat_index=? "
                    "AND state='running'",
                    (row["task_id"], row["repeat_index"]),
                )
                stats["attempts_abandoned"] += 1
                stats["slots_reset"] += 1
            connection.execute(
                "UPDATE tasks SET state='pending',owner=NULL WHERE state='running'"
            )
            stats["tasks_reset"] = connection.execute(
                "SELECT changes()"
            ).fetchone()[0]
            connection.execute(
                "UPDATE workers SET pid=NULL,pid_start=NULL,state='idle',current_task=NULL,"
                "current_repeat=0,current_phase=NULL"
            )

        with contextlib.closing(connect(self.path)) as db:
            write_transaction(db, operation)
        return stats

    def recover_finished_from_receipts(
        self, verify: Callable[[sqlite3.Row], bool], required_stages: list[int]
    ) -> dict[str, int]:
        """崩溃窗口恢复：结果已落盘、DB 尚未提交的 attempt 补记完成。

        verify 回调验证 attempt 输出目录中的 receipt（hash、文件集合、
        阶段覆盖）；只有验证通过的才补记，避免重复调用模型。verify 为
        False 的保持 abandoned 待重跑。DB 已完成但结果缺失的情况由调用
        方另行报告为异常。
        """
        stats = {"recovered": 0}
        for attempt in self.active_attempts():
            if verify(attempt):
                outcome = self.recover_dead_attempt(attempt, "recovered_from_receipt", receipt_valid=True)
                if outcome == "finished":
                    stats["recovered"] += 1
        return stats

    def verify_done_results_exist(self) -> list[str]:
        """核对已完成 slot 的输出目录仍然存在；缺失则明确报告异常。"""
        problems: list[str] = []
        with contextlib.closing(connect(self.path, readonly=True)) as db:
            rows = db.execute(
                "SELECT s.finished_attempt_id AS attempt_id, a.output_dir, t.task_path "
                "FROM repeat_slots s JOIN tasks t ON t.id=s.task_id "
                "LEFT JOIN attempts a ON a.attempt_id=s.finished_attempt_id "
                "WHERE s.state='done'"
            )
            for row in rows:
                output_dir = row["output_dir"]
                if not output_dir or not Path(output_dir, "result.json").exists():
                    problems.append(
                        f"{row['task_path']}:{row['attempt_id']}:result_missing"
                    )
        return problems

    # ---- supervisor 与 launch 记录 ------------------------------------

    def begin_launch(
        self, launch_id: str, supervisor_pid: int, workers: int, capture_dir: str
    ) -> None:
        with contextlib.closing(connect(self.path)) as db:
            def operation(connection: sqlite3.Connection) -> None:
                connection.execute(
                    "INSERT INTO launches(id,started,supervisor_pid,supervisor_pid_start,"
                    "boot_id,workers,capture_dir,state) VALUES(?,?,?,?,?,?,?,'running')",
                    (
                        launch_id,
                        time.time(),
                        supervisor_pid,
                        util.pid_start_ticks(supervisor_pid),
                        util.boot_id(),
                        workers,
                        capture_dir,
                    ),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES('controller_pid',?)",
                    (str(supervisor_pid),),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES('controller_pid_start',?)",
                    (util.pid_start_ticks(supervisor_pid),),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES('controller_boot_id',?)",
                    (util.boot_id(),),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES('controller_state','running')"
                )
                connection.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES('current_launch',?)",
                    (launch_id,),
                )

            write_transaction(db, operation)

    def finish_launch(self, state: str) -> None:
        with contextlib.closing(connect(self.path)) as db:
            def operation(connection: sqlite3.Connection) -> None:
                launch_id = self.meta_get(connection, "current_launch")
                if launch_id:
                    connection.execute(
                        "UPDATE launches SET finished=?,state=? WHERE id=?",
                        (time.time(), state, launch_id),
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES('controller_state',?)",
                    (state,),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES('controller_heartbeat',?)",
                    (str(time.time()),),
                )

            write_transaction(db, operation)

    def heartbeat(self) -> None:
        with contextlib.closing(connect(self.path)) as db:
            write_transaction(
                db,
                lambda c: c.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES('controller_heartbeat',?)",
                    (str(time.time()),),
                ),
            )

    def register_worker(self, rank: int, pid: int) -> None:
        with contextlib.closing(connect(self.path)) as db:
            write_transaction(
                db,
                lambda c: c.execute(
                    "UPDATE workers SET pid=?,pid_start=?,state='idle',heartbeat=?,"
                    "started=?,error=NULL WHERE rank=?",
                    (pid, util.pid_start_ticks(pid), time.time(), time.time(), rank),
                ),
            )

    def update_worker(self, rank: int, state: str, error: str | None = None) -> None:
        with contextlib.closing(connect(self.path)) as db:
            write_transaction(
                db,
                lambda c: c.execute(
                    "UPDATE workers SET state=?,heartbeat=?,error=? WHERE rank=?",
                    (state, time.time(), error, rank),
                ),
            )

    def recover_dead_worker(self, rank: int, exitcode: int | None) -> None:
        """异常 worker 归位：task 回 pending、slot 由 attempt 恢复流程处理。"""
        message = f"worker rank={rank} exited unexpectedly (exit={exitcode})"
        with contextlib.closing(connect(self.path)) as db:
            def operation(connection: sqlite3.Connection) -> None:
                connection.execute(
                    "UPDATE tasks SET state='pending',owner=NULL,error=? "
                    "WHERE owner=? AND state='running'",
                    (message, rank),
                )
                connection.execute(
                    "UPDATE workers SET pid=NULL,pid_start=NULL,state='restarting',"
                    "current_task=NULL,current_repeat=0,current_phase=NULL,error=?,"
                    "restarts=restarts+1 WHERE rank=?",
                    (message, rank),
                )

            write_transaction(db, operation)

    def all_workers_dead(self) -> bool:
        with contextlib.closing(connect(self.path, readonly=True)) as db:
            for row in db.execute("SELECT pid,pid_start FROM workers"):
                if row["pid"] and row["pid_start"] and util.pid_start_ticks(row["pid"]) == row["pid_start"]:
                    return False
            return True

    def unfinished_task_count(self) -> int:
        with contextlib.closing(connect(self.path, readonly=True)) as db:
            return db.execute("SELECT COUNT(*) FROM tasks WHERE state!='done'").fetchone()[0]

    def is_complete(self) -> bool:
        with contextlib.closing(connect(self.path, readonly=True)) as db:
            return (
                db.execute("SELECT COUNT(*) FROM tasks WHERE state!='done'").fetchone()[0] == 0
            )

    # ---- 供后处理读取 ---------------------------------------------------

    def finished_attempts(self) -> list[dict[str, Any]]:
        with contextlib.closing(connect(self.path, readonly=True)) as db:
            rows = db.execute(
                "SELECT a.*,t.task_path,t.mode FROM attempts a JOIN tasks t ON t.id=a.task_id "
                "WHERE a.state='finished' ORDER BY t.id,a.repeat_index,a.id"
            )
            return [dict(row) for row in rows]

    def stage_results(self, attempt_ids: list[str]) -> dict[str, dict[int, dict[str, Any]]]:
        result: dict[str, dict[int, dict[str, Any]]] = {}
        if not attempt_ids:
            return result
        with contextlib.closing(connect(self.path, readonly=True)) as db:
            placeholders = ",".join("?" for _ in attempt_ids)
            for row in db.execute(
                f"SELECT * FROM stage_results WHERE attempt_id IN ({placeholders}) "
                "ORDER BY attempt_id,stage",
                attempt_ids,
            ):
                result.setdefault(row["attempt_id"], {})[int(row["stage"])] = dict(row)
        return result

    def set_image_cleanup(self, image: str, state: str, image_id: str | None, error: str | None) -> None:
        with contextlib.closing(connect(self.path)) as db:
            write_transaction(
                db,
                lambda c: c.execute(
                    "INSERT INTO image_cleanup(image,state,updated,image_id,error) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(image) DO UPDATE SET "
                    "state=excluded.state,updated=excluded.updated,image_id=excluded.image_id,"
                    "error=excluded.error",
                    (image, state, time.time(), image_id, error),
                ),
            )
