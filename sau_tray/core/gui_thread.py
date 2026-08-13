"""
sau_tray.core.gui_thread
~~~~~~~~~~~~~~~~~~~~~~~~
专用 GUI 线程管理（解决 tkinter 必须在主线程运行的 Windows 限制）。

从 tray_app.py 提取。所有 tkinter 操作都在这个线程中执行。
pystray 菜单回调在 pystray 内部线程执行，不能直接创建/操作 tkinter 窗口。
通过 queue 向 GUI 线程发送命令，GUI 线程用 after() 轮询执行。

严格单向依赖：core.gui_thread → core.config（仅 logger）。
"""
from __future__ import annotations

import queue
import threading
from typing import Any

from sau_tray.core.config import logger

# ---------------------------------------------------------------------------
# GUI 线程内部状态
# ---------------------------------------------------------------------------
_gui_thread: threading.Thread | None = None
_gui_queue: queue.Queue[Any] | None = None
_gui_root: Any = None  # tkinter.Tk 实例（在 GUI 线程中创建）
_gui_ready = threading.Event()  # GUI 线程就绪标志

# 退出清理标志（避免 _on_exit_tray 和 run_tray finally 重复清理）
_cleanup_done: bool = False
_cleanup_lock = threading.Lock()

# 登录线程引用追踪（退出时优雅等待）
_login_threads: list[threading.Thread] = []
_login_threads_lock = threading.Lock()


# ---------------------------------------------------------------------------
# GUI 线程主函数
# ---------------------------------------------------------------------------
def _gui_thread_main() -> None:
    """GUI 线程主函数：创建隐藏的 Tk root 窗口并运行 mainloop。

    通过 _gui_queue 接收命令，用 after() 轮询执行，确保所有 tkinter
    操作都在这个线程中完成，符合 Windows GUI 线程模型。
    """
    import tkinter as tk

    global _gui_root
    root = tk.Tk()
    _gui_root = root

    # 隐藏主窗口（仅作为 tkinter mainloop 的宿主）
    root.withdraw()

    # 标记 GUI 线程就绪
    _gui_ready.set()

    def _process_queue() -> None:
        """轮询处理 GUI 命令队列。"""
        if _gui_queue is None:
            return
        try:
            while True:
                cmd = _gui_queue.get_nowait()
                try:
                    cmd(root)
                except Exception as e:
                    logger.error("GUI 命令执行失败: %s", e)
        except queue.Empty:
            pass
        # 继续轮询（每 50ms 检查一次队列）
        if root.winfo_exists():
            root.after(50, _process_queue)
        else:
            logger.warning("GUI root 已销毁，主动退出 mainloop")
            try:
                root.quit()
            except Exception:
                pass

    # 启动队列轮询
    root.after(50, _process_queue)

    # 运行 mainloop（阻塞，直到 root.quit() 被调用）
    try:
        root.mainloop()
    except Exception as e:
        logger.error("GUI mainloop 异常退出: %s", e)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def start() -> None:
    """启动专用 GUI 线程并等待其就绪。"""
    global _gui_thread, _gui_queue
    _gui_queue = queue.Queue()
    _gui_ready.clear()
    _gui_thread = threading.Thread(
        target=_gui_thread_main, daemon=True, name="SAU-GUI-Thread",
    )
    _gui_thread.start()
    # 等待 GUI 线程完成初始化（最多 5 秒）
    if not _gui_ready.wait(timeout=5.0):
        logger.error("GUI 线程启动超时")


def stop() -> None:
    """停止 GUI 线程。

    优先通过 queue 调度 quit（线程安全），如果队列方式超时未生效，
    使用 Win32 PostMessageW 作为兜底（Windows 官方推荐的跨线程窗口操作方式）。
    """
    global _gui_root, _gui_thread

    # 优先通过队列调度（线程安全）
    if _gui_queue is not None and _gui_thread is not None and _gui_thread.is_alive():
        _gui_queue.put(lambda root: root.quit())
        if _gui_thread is not None:
            _gui_thread.join(timeout=3.0)

    # 如果队列方式未生效，使用 PostMessageW 兜底
    if _gui_thread is not None and _gui_thread.is_alive() and _gui_root is not None:
        try:
            import ctypes
            user32 = ctypes.windll.user32
            # 声明类型避免 64 位下句柄被默认 c_int 截断
            user32.GetParent.argtypes = (ctypes.c_void_p,)
            user32.GetParent.restype = ctypes.c_void_p
            user32.PostMessageW.argtypes = (
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_long,
            )
            user32.PostMessageW.restype = ctypes.c_int
            hwnd = _gui_root.winfo_id()
            # winfo_id() 可能返回子窗口句柄，WM_CLOSE 需发给 toplevel：
            # 先取 GetParent，再回退 wm_frame（Tk 顶层窗口句柄）
            parent = ctypes.windll.user32.GetParent(hwnd)
            if parent:
                hwnd = parent
            else:
                try:
                    frame = _gui_root.wm_frame()
                    hwnd = int(frame, 16)
                except (TypeError, ValueError):
                    try:
                        hwnd = int(_gui_root.wm_frame())
                    except (TypeError, ValueError):
                        pass
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            _gui_thread.join(timeout=2.0)
        except Exception:
            logger.warning("GUI 线程 PostMessageW 兜底失败", exc_info=True)

    _gui_root = None
    _gui_thread = None


def schedule(func: Any) -> None:
    """向 GUI 线程调度一个 callable（线程安全）。

    func 签名为 ``func(root)`` ，在 GUI 线程的 mainloop 中被调用。
    GUI 线程未运行时记录 warning 并丢弃任务。
    """
    if _gui_queue is None or _gui_thread is None or not _gui_thread.is_alive():
        logger.warning("GUI 线程未运行，丢弃调度任务: %s", getattr(func, '__name__', str(func)[:50]))
        return
    _gui_queue.put(func)


# ---------------------------------------------------------------------------
# 属性访问（线程安全）
# ---------------------------------------------------------------------------
def get_root() -> Any:
    """返回 tkinter.Tk 实例（可能为 None）。"""
    return _gui_root


def get_thread() -> threading.Thread | None:
    """返回 GUI 线程引用（可能为 None）。"""
    return _gui_thread


def is_cleanup_done() -> bool:
    """退出清理是否已完成。"""
    with _cleanup_lock:
        return _cleanup_done


def set_cleanup_done(value: bool = True) -> None:
    """标记退出清理已完成。"""
    global _cleanup_done
    with _cleanup_lock:
        _cleanup_done = value


def get_cleanup_lock() -> threading.Lock:
    """返回清理锁（供外部 with 块使用）。"""
    return _cleanup_lock


def add_login_thread(t: threading.Thread) -> None:
    """注册一个登录线程引用。"""
    with _login_threads_lock:
        _login_threads.append(t)


def get_login_threads() -> list[threading.Thread]:
    """返回登录线程列表快照。"""
    with _login_threads_lock:
        return list(_login_threads)


def get_login_threads_lock() -> threading.Lock:
    """返回登录线程锁。"""
    return _login_threads_lock
