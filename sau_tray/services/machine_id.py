"""
sau_tray.services.machine_id
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
机器码获取服务（带缓存，线程安全）。

从 tray_app.py 提取。可独立调用，不依赖 tray_app.py。
"""
from __future__ import annotations

import threading


# ---------------------------------------------------------------------------
# 机器码缓存（启动时计算一次，避免每次打开设置页面都调用 WMI/PowerShell）
# ---------------------------------------------------------------------------
_machine_code_cache: str | None = None
_machine_code_lock = threading.Lock()


def get_machine_code() -> str:
    """获取机器码（带缓存，线程安全）。

    首次调用时计算并缓存，后续直接返回缓存值。
    """
    global _machine_code_cache
    with _machine_code_lock:
        if _machine_code_cache is not None:
            return _machine_code_cache
        try:
            from sau_agent_pkg.machine import get_machine_code as _real_get
            _machine_code_cache = _real_get()
        except Exception:
            _machine_code_cache = "N/A"
        return _machine_code_cache


def preload() -> None:
    """在应用启动时预加载机器码（后台线程调用，避免阻塞主流程）。"""
    try:
        get_machine_code()
    except Exception:
        pass
