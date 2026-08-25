# -*- coding: utf-8 -*-
"""本地 SQLite 存储（现状文档 §5.5 建表、WAL 模式）。

两表：
- ``local_tasks``：接收到的发布任务（publish_task 落库；本步仅落库骨架，真实执行后续步骤）；
- ``result_queue``：task_result 持久化补发队列——发送前先落库，服务端确认后删除（§5.4）。

连接策略：每操作短连接 + 线程锁（服务进程单事件循环 + 偶发 CLI 访问，简单可靠）。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

from sau_wrap import paths

_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_tasks (
    task_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    run_at TEXT,
    attempts INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS result_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect() -> sqlite3.Connection:
    paths.ensure_db_dir()
    conn = sqlite3.connect(paths.DB_FILE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def db_init() -> None:
    """建表（幂等）并启用 WAL。服务启动 / doctor 时调用。"""
    with _LOCK, _connect() as conn:
        conn.executescript(_SCHEMA)


# ---------------------------------------------------------------- local_tasks


def upsert_task(task_id: str, payload: str, status: str = "queued", run_at: str | None = None) -> None:
    """publish_task 落库（幂等：同 task_id 已存在则保留原记录，仅刷新 updated_at）。"""
    now = _utcnow()
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO local_tasks (task_id, payload, status, run_at, attempts, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (task_id, payload, status, run_at, now, now),
        )


def count_active_tasks() -> int:
    """执行中任务数（心跳 active_tasks 字段；queued/running 视为活跃）。"""
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM local_tasks WHERE status IN ('queued', 'running')"
        ).fetchone()
    return int(row[0] or 0)


def set_task_status(task_id: str, status: str) -> None:
    now = _utcnow()
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE local_tasks SET status = ?, updated_at = ? WHERE task_id = ?",
            (status, now, task_id),
        )


def get_task(task_id: str) -> tuple[str, str, str] | None:
    """按 task_id 读取（task_id, payload, status）；不存在返回 None。"""
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT task_id, payload, status FROM local_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    if row is None:
        return None
    return (str(row[0]), str(row[1]), str(row[2]))


def list_pending_tasks() -> list[tuple[str, str]]:
    """重启恢复（§5.3）：返回所有 queued/running 任务的 (task_id, payload)。"""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT task_id, payload FROM local_tasks WHERE status IN ('queued', 'running')"
            " ORDER BY created_at"
        ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def update_task_payload(task_id: str, payload: str) -> None:
    """更新任务载荷（素材重签后新 URL 回写，防重启后丢失）。"""
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE local_tasks SET payload = ?, updated_at = ? WHERE task_id = ?",
            (payload, _utcnow(), task_id),
        )


def bump_task_attempts(task_id: str) -> int:
    """执行尝试计数 +1，返回新值（供排障/后续重试策略）。"""
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE local_tasks SET attempts = attempts + 1, updated_at = ? WHERE task_id = ?",
            (_utcnow(), task_id),
        )
        row = conn.execute(
            "SELECT attempts FROM local_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------- result_queue


def enqueue_result(task_id: str, payload: str) -> int:
    """task_result 发送前先落库（§5.4 语义），返回队列项 id。"""
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO result_queue (task_id, payload, created_at) VALUES (?, ?, ?)",
            (task_id, payload, _utcnow()),
        )
        return int(cur.lastrowid)


def peek_results(limit: int = 50) -> list[tuple[int, str, str]]:
    """读取待补发队列项（id, task_id, payload），按入队顺序。"""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT id, task_id, payload FROM result_queue ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
    return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]


def delete_result(item_id: int) -> None:
    """服务端确认（发送成功）后删除队列项。"""
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM result_queue WHERE id = ?", (item_id,))
