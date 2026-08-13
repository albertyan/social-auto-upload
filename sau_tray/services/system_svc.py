"""
sau_tray.services.system_svc
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
系统级服务操作：单实例锁、UAC 提权服务控制、进程清理。

从 tray_app.py 提取。严格单向依赖：不依赖 tray_app.py。
所有 subprocess 调用保持 CREATE_NO_WINDOW。
ShellExecuteW 保持 SW_HIDE(0)。
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 单实例锁（防止 sau-tray.exe 重复启动）
# ---------------------------------------------------------------------------
_SINGLE_INSTANCE_MUTEX_NAME = "Global\\SAU-Tray-SingleInstance"
_single_instance_handle: Any = None


def acquire_single_instance() -> bool:
    """尝试获取单实例互斥锁。

    返回 True 表示成功（首次启动），False 表示已有实例在运行。
    """
    global _single_instance_handle
    try:
        _single_instance_handle = ctypes.windll.kernel32.CreateMutexW(
            None,  # 默认安全描述符
            1,     # 立即获取所有权
            _SINGLE_INSTANCE_MUTEX_NAME,
        )
        if not _single_instance_handle:
            return False
        last_error = ctypes.windll.kernel32.GetLastError()
        if last_error == 183:  # ERROR_ALREADY_EXISTS
            ctypes.windll.kernel32.CloseHandle(_single_instance_handle)
            _single_instance_handle = None
            return False
        return True
    except Exception:
        return True  # 互斥锁失败时不阻止启动


def release_single_instance() -> None:
    """释放单实例互斥锁。"""
    global _single_instance_handle
    if _single_instance_handle:
        try:
            ctypes.windll.kernel32.ReleaseMutex(_single_instance_handle)
            ctypes.windll.kernel32.CloseHandle(_single_instance_handle)
        except Exception:
            pass
        _single_instance_handle = None


# ---------------------------------------------------------------------------
# UAC 提权服务控制
# ---------------------------------------------------------------------------
def elevated_service_control(action: str) -> None:
    """通过 ShellExecute "runas" 提权执行服务控制命令（触发 UAC）。

    使用 SW_HIDE(0) 避免 CMD 窗口闪烁。
    """
    exe_path = str(Path(sys.executable).resolve().parent / "sau-service.exe")
    if not Path(exe_path).is_file():
        raise FileNotFoundError(f"找不到服务程序: {exe_path}")

    # [修复 #2a] 使用 SW_HIDE(0) 替代 SW_SHOWNORMAL(1)，避免 CMD 窗口闪烁
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", exe_path, action, None, 0,  # SW_HIDE
    )
    if result <= 32:
        raise RuntimeError(f"ShellExecute 失败 (code={result})")


# ---------------------------------------------------------------------------
# 进程清理
# ---------------------------------------------------------------------------
def kill_sau_processes() -> None:
    """终止所有 SAU 相关进程（排除自身）。

    使用 /T 终止进程树，确保子进程也被清理。
    分两轮执行：第一轮正常终止，第二轮补刀残留进程。
    所有 taskkill 调用使用 CREATE_NO_WINDOW。
    """
    sau_names = [
        "sau.exe", "sau-ops.exe", "sau-service.exe",
        "sau-tray.exe", "sau_backend.exe",
        "sau_agent.exe", "sau-agent.exe",
        "sau_cli.exe", "sau_ops.exe",
    ]
    for name in sau_names:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/IM", name],
                capture_output=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass
    # 第二轮：短暂等待后补刀（处理第一轮中正在退出但尚未完全终止的进程）
    time.sleep(0.5)
    for name in sau_names:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/IM", name],
                capture_output=True, timeout=3,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass
