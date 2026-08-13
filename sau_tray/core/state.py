"""
sau_tray.core.state
~~~~~~~~~~~~~~~~~~~
线程安全的共享状态管理。

从 tray_app.py 提取，封装为模块级函数 + Lock 保护。
严格单向依赖：core.state → core.config（仅 logger）。
"""
from __future__ import annotations

import threading
from typing import Any

from sau_tray.core.config import logger  # noqa: F401  (re-export for convenience)

# ---------------------------------------------------------------------------
# 状态管理（线程安全）
# ---------------------------------------------------------------------------
_STATUS_LOCK = threading.Lock()
_current_status: dict[str, Any] = {
    "service_running": False,
    "ws_connected": False,
    "agent_id": "",
    "version": "",
    "active_tasks": 0,
    "accounts": [],
    "clock_offset_seconds": 0.0,
    "clock_sync_status": "unknown",
    "token_expire_at": None,
    "token_remaining_days": None,
    "token_status": "unknown",
    "updating": False,
}


def get_status() -> dict[str, Any]:
    """获取当前状态的快照（线程安全，返回副本）。"""
    with _STATUS_LOCK:
        return dict(_current_status)


def set_status(data: dict[str, Any]) -> None:
    """批量更新状态字段（线程安全）。"""
    with _STATUS_LOCK:
        _current_status.update(data)
