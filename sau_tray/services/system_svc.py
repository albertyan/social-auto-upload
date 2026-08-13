"""
sau_tray.services.system_svc
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
系统级服务操作：单实例锁、UAC 提权服务控制、进程清理。

从 tray_app.py 提取。严格单向依赖：不依赖 tray_app.py。
所有 subprocess 调用保持 CREATE_NO_WINDOW。
ShellExecuteW 保持 SW_HIDE(0)。
"""
from __future__ import annotations

import csv
import ctypes
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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
    # 幂等保护：为什么必须加这个判断：
    # tray_app.py 会调用两次 acquire_single_instance：
    #   1) main() L313  — 入口处拦截重复启动（已存在就 MessageBoxW + 直接退出）
    #   2) run()  L244  — 启动关键日志快照（要把锁状态打到启动 banner 里）
    # 如果不加幂等，第二次调用对同一个互斥体名字 CreateMutexW 时，
    # GetLastError() 会返回 183 (ERROR_ALREADY_EXISTS) —— 这不是"其他实例在运行"，
    # 而是"同进程里同名互斥体已存在"。但原来代码把 183 一律当成"已有其他实例"，
    # 结果就会 CloseHandle + 把 _single_instance_handle 置 None，
    # 把第一次调用正确持有的句柄冲掉（release_single_instance 就失效了），
    # 同时日志里还会误报"检测到已有实例运行"，启动 banner 里 single_instance_lock=False。
    if _single_instance_handle is not None:
        logger.debug("acquire_single_instance: 已持有互斥锁句柄，跳过重复创建（幂等保护命中）")  # 为什么打 debug：幂等保护正常命中时无需用户关心，只在 debug 追踪时看
        return True
    try:
        _single_instance_handle = ctypes.windll.kernel32.CreateMutexW(
            None,  # 默认安全描述符
            1,     # 立即获取所有权
            _SINGLE_INSTANCE_MUTEX_NAME,
        )
        if not _single_instance_handle:
            logger.info("acquire_single_instance: 创建互斥锁句柄失败，返回 False")  # 为什么打这条日志：记录互斥锁创建失败
            return False
        last_error = ctypes.windll.kernel32.GetLastError()
        if last_error == 183:  # ERROR_ALREADY_EXISTS
            ctypes.windll.kernel32.CloseHandle(_single_instance_handle)
            _single_instance_handle = None
            logger.info("acquire_single_instance: 失败，检测到已有实例运行 (ERROR_ALREADY_EXISTS=183)")  # 为什么打这条日志：确认已有实例导致启动被拦截
            return False
        logger.info("acquire_single_instance: 成功获取单实例互斥锁")  # 为什么打这条日志：确认互斥锁获取成功，启动是首次
        return True
    except Exception as e:
        logger.info("acquire_single_instance: 互斥锁 API 异常，放行启动: %s", e)  # 为什么打这条日志：记录互斥锁异常但不阻止启动的场景
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
    logger.info("elevated_service_control: ShellExecuteW action=%s, result=%d", action, result)  # 为什么打这条日志：记录 ShellExecute 结果（含提权情况）
    if result <= 32:
        logger.error("elevated_service_control: ShellExecuteW 失败 action=%s, code=%d（≤32 表示失败）",  # 为什么打这条日志：≤32 为失败码，error 级记录带 code
                     action, result)
        raise RuntimeError(f"ShellExecute 失败 (code={result})")


# ---------------------------------------------------------------------------
# 进程清理
# ---------------------------------------------------------------------------
def _get_pids_by_name(name: str) -> list[int]:
    """用 tasklist 查询指定进程名的全部 PID。

    用 csv.reader 解析（兼容区域设置导致的分隔符差异，如 zh-CN 下
    tasklist CSV 可能使用逗号但内存列含千分位逗号），不做手工 split。
    """
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:
        logger.error("kill_sau: tasklist 查询 %s 异常: %s", name, e)
        return []
    pids: list[int] = []
    # CSV 格式: "sau-tray.exe","1234","Console","1","1,234 K"
    for row in csv.reader(r.stdout.decode(errors="replace").splitlines()):
        if len(row) >= 2 and row[0].lower() == name.lower():
            try:
                pids.append(int(row[1]))
            except ValueError:
                pass
    return pids


def _verify_process_image(pid: int, expect_name: str) -> bool | None:
    """kill 前复核目标 PID 的映像名，防止 PID 复用误杀无关进程树。

    tasklist 枚举与 taskkill 之间存在时间窗，PID 可能被系统复用。
    返回 True = 映像名匹配；False = 不匹配（PID 已被复用）；
    None = 无法查询（进程已退出或无权限），按已退出处理。
    """
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = (ctypes.c_uint, ctypes.c_int, ctypes.c_uint)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.QueryFullProcessImageNameW.argtypes = (
        ctypes.c_void_p, ctypes.c_ulong,
        ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong),
    )
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_ulong(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        image = Path(buf.value).name.lower()
        return image == expect_name.lower()
    except Exception:
        return None
    finally:
        kernel32.CloseHandle(handle)


def _kill_pid(name: str, pid: int) -> bool:
    """taskkill /F /T /PID 强杀单个进程树；返回是否成功。

    kill 前用 QueryFullProcessImageNameW 复核映像名，不匹配则跳过
    （视为目标已退出，PID 被复用）。
    """
    match = _verify_process_image(pid, name)
    if match is False:
        logger.debug("kill_sau: PID=%d 映像名已不是 %s（PID 被复用），跳过误杀", pid, name)
        return True
    if match is None:
        logger.debug("kill_sau: PID=%d 已无法查询（视为已退出），跳过", pid)
        return True
    try:
        r = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:
        logger.error("kill_sau: taskkill %s (PID=%d) 异常: %s", name, pid, e)
        return False
    if r.returncode in (0, 128):  # 128 = 进程已不存在（正在退出）
        return True
    detail = r.stderr.decode(errors="replace").strip() or r.stdout.decode(errors="replace").strip()
    logger.warning(
        "kill_sau: %s (PID=%d) 终止失败 rc=%d（可能 Access Denied，如 SYSTEM 服务进程）: %s",
        name, pid, r.returncode, detail,
    )
    return False


def kill_sau_processes() -> None:
    """终止所有 SAU 相关进程（按 PID，排除自身）。

    旧实现用 taskkill /IM 按名杀，会杀掉 sau-tray.exe 自身导致进程立即
    死亡，后续清理全部成为死代码。新实现：先按进程名查 PID，逐个
    taskkill /F /T /PID，跳过 os.getpid() 自身（自身交给 icon.stop()
    正常退出路径）。使用 /T 终止进程树；两轮执行：第二轮补刀残留。
    """
    sau_names = [
        "sau.exe", "sau-ops.exe", "sau-service.exe",
        "sau-tray.exe", "sau_backend.exe",
        "sau_agent.exe", "sau-agent.exe",
        "sau_cli.exe", "sau_ops.exe",
    ]
    self_pid = os.getpid()
    killed: list[tuple[str, int]] = []
    failed: list[tuple[str, int]] = []

    def _sweep(sweep_index: int) -> None:
        sweep_killed_before = len(killed)
        sweep_failed_before = len(failed)
        logger.info("kill_sau: 第 %d 轮执行开始，待扫描进程名 %d 个", sweep_index, len(sau_names))  # 为什么打这条日志：追踪两轮清理的执行顺序
        for name in sau_names:
            pids = _get_pids_by_name(name)
            if pids:
                logger.debug("kill_sau: 第 %d 轮发现进程 %s: PIDs=%s", sweep_index, name, pids)
            for pid in pids:
                if pid == self_pid:
                    logger.info("kill_sau: 第 %d 轮跳过自身进程（误杀跳过） %s (PID=%d)", sweep_index, name, pid)  # 为什么打这条日志：明确自身进程被跳过的轮次
                    continue
                if _kill_pid(name, pid):
                    killed.append((name, pid))
                else:
                    failed.append((name, pid))
        round_killed = len(killed) - sweep_killed_before
        round_failed = len(failed) - sweep_failed_before
        logger.info("kill_sau: 第 %d 轮执行结束，本论 success=%d, failed=%d", sweep_index, round_killed, round_failed)  # 为什么打这条日志：每轮汇总成功/失败数

    _sweep(1)
    # 第二轮：短暂等待后补刀（处理第一轮中正在退出但尚未完全终止的进程）
    time.sleep(0.5)
    _sweep(2)
    logger.info("kill_sau: 两轮清理全部完成，累计 success=%d, failed=%d，失败列表: %s",  # 为什么打这条日志：两轮汇总最终结果
                len(killed), len(failed), failed or "[]")
