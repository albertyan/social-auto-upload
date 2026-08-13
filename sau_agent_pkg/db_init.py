"""
sau_agent_pkg.db_init
~~~~~~~~~~~~~~~~~~~~~
SQLite 数据库初始化模块。

数据库路径：SAU_HOME / "db" / "sau.db"

表结构：
- local_tasks：本地任务记录（断线补发、定时调度持久化）
- result_queue：结果队列（断线期间暂存 task_result，重连后补发）
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from sau_agent_pkg.config import SAU_HOME


# 数据库路径
DB_PATH: Path = SAU_HOME / "db" / "sau.db"


# ---------------------------------------------------------------------------
# SQL 建表语句
# ---------------------------------------------------------------------------

_CREATE_LOCAL_TASKS = """
CREATE TABLE IF NOT EXISTS local_tasks (
    task_id     TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,          -- JSON 格式的任务内容
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued/running/success/failed
    run_at      INTEGER,                -- 定时执行时间戳（Unix 秒），NULL 表示立即执行
    attempts    INTEGER NOT NULL DEFAULT 0,     -- 已尝试次数
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_RESULT_QUEUE = """
CREATE TABLE IF NOT EXISTS result_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    payload     TEXT NOT NULL,          -- JSON 格式的结果内容
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# 索引：按状态查询任务、按 task_id 查询结果
_CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_local_tasks_status ON local_tasks(status);
CREATE INDEX IF NOT EXISTS idx_local_tasks_run_at ON local_tasks(run_at);
CREATE INDEX IF NOT EXISTS idx_result_queue_task_id ON result_queue(task_id);
"""


def init_db() -> Path:
    """
    初始化数据库：创建目录 + 建表 + 创建索引。

    Returns:
        数据库文件路径。
    """
    # 确保目录存在
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # 连接并建表
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cursor = conn.cursor()
        cursor.execute(_CREATE_LOCAL_TASKS)
        cursor.execute(_CREATE_RESULT_QUEUE)
        cursor.executescript(_CREATE_INDEXES)
        conn.commit()
    finally:
        conn.close()

    return DB_PATH


def get_connection() -> sqlite3.Connection:
    """
    获取数据库连接（调用方需自行 close）。
    启用 WAL 模式以支持并发读写。
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


if __name__ == "__main__":
    # 方便调试：直接运行初始化数据库
    path = init_db()
    print(f"Database initialized at: {path}")
