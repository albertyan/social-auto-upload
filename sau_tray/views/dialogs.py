"""
sau_tray.views.dialogs
~~~~~~~~~~~~~~~~~~~~~~
简单对话框工具函数。

- show_notify: 使用 pystray icon.notify() 的系统通知（线程安全）
- show_confirm: 确认对话框（通过 GUI 线程调度）
"""
from __future__ import annotations

import ctypes
from typing import Any

from sau_tray.core import gui_thread

# ---------------------------------------------------------------------------
# 全局 icon 引用（由 tray_app.run_tray() 初始化）
# ---------------------------------------------------------------------------
_icon_ref: Any = None


def set_icon(icon: Any) -> None:
    """保存 pystray icon 引用（在 run_tray 中调用）。"""
    global _icon_ref
    _icon_ref = icon


def get_icon() -> Any:
    """返回当前 pystray icon 引用。"""
    return _icon_ref


# ---------------------------------------------------------------------------
# 系统通知（线程安全）
# ---------------------------------------------------------------------------
def show_notify(message: str, title: str = "SAU") -> None:
    """显示 Windows Toast 通知（线程安全，使用 pystray Icon.notify）。

    可从任意线程调用，不会阻塞调用线程。
    """
    try:
        if _icon_ref is not None:
            _icon_ref.notify(message, title)
        else:
            print(f"[{title}] {message}")
    except Exception:
        print(f"[{title}] {message}")



# ---------------------------------------------------------------------------
# 确认对话框
# ---------------------------------------------------------------------------
def show_confirm(message: str, title: str = "确认") -> bool:
    """是/否确认对话框（ctypes MessageBoxW，可从任意线程调用）。

    使用 Win32 MessageBoxW，不依赖 tkinter，线程安全。
    """
    try:
        MB_YESNO_ICONQUESTION = 0x4 | 0x20  # MB_YESNO | MB_ICONQUESTION
        ret = ctypes.windll.user32.MessageBoxW(0, message, title, MB_YESNO_ICONQUESTION)
        return ret == 6  # IDYES
    except Exception:
        return False
