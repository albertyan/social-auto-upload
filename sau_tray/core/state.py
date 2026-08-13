"""
sau_tray.core.state
~~~~~~~~~~~~~~~~~~~
线程安全的共享状态管理。

从 tray_app.py 提取，封装为模块级函数 + Lock 保护。
严格单向依赖：core.state → core.config（仅 logger）。
"""
from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

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

# 需要监控变化的关键字段（变化时打 info 日志对比前后）
_WATCH_FIELDS = ("service_running", "ws_connected", "token_status")


def get_status() -> dict[str, Any]:
    """获取当前状态的快照（线程安全，返回副本）。"""
    logger.debug("state.get_status: 调用状态查询")  # 为什么打这条日志：debug 追踪状态读取频率
    with _STATUS_LOCK:
        return dict(_current_status)


def set_status(data: dict[str, Any]) -> None:
    """批量更新状态字段（线程安全）。"""
    keys = sorted(data.keys())
    logger.debug("state.set_status: 调用状态更新，字段=%s", keys)  # 为什么打这条日志：debug 追踪状态写入的字段
    with _STATUS_LOCK:
        changed_details = []
        for f in _WATCH_FIELDS:
            if f in data:
                old = _current_status.get(f)
                new = data[f]
                if old != new:
                    changed_details.append(f"{f}: {old!r} -> {new!r}")
        _current_status.update(data)
        if changed_details:
            logger.info("state.set_status: 关键字段变化 %s", "; ".join(changed_details))  # 为什么打这条日志：info 级别追踪核心状态前后对比（服务运行/WS连接/Token 状态）
