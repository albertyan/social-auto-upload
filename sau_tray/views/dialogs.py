"""
sau_tray.views.dialogs
~~~~~~~~~~~~~~~~~~~~~~
简单对话框工具函数。

- show_notify:   使用 pystray icon.notify() 的系统通知（线程安全，短提示）
- show_info:     信息展示弹框（单确定按钮，屏幕右下角任务栏旁，多行无截断，任意线程可调用）
- show_confirm:  是/否确认弹框（屏幕右下角任务栏旁，任意线程可调用，返回布尔结果）

所有 Win32 MessageBoxW 都统一调度到 GUI 线程（带 Tk mainloop），
并安装 WH_CBT 钩子让弹框默认落在「屏幕右下角靠近托盘/任务栏的位置」
（这是用户偏好，和 Settings 窗口默认位置保持一致的视觉习惯）。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import threading
from typing import Any

from sau_tray.core import gui_thread

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Win32 常量声明
# ---------------------------------------------------------------------------
_HCBT_ACTIVATE = 5
_WH_CBT = 5

_MB_OK = 0x0
_MB_YESNO = 0x4
_MB_ICONINFORMATION = 0x40
_MB_ICONQUESTION = 0x20
_MB_SYSTEMMODAL = 0x1000  # 模态级别更高，保证不会被其他窗口挡住（不影响关闭）

_IDOK = 1
_IDYES = 6
_IDNO = 7

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
# 弹框右下角定位（WH_CBT 钩子 + SetWindowPos）
# ---------------------------------------------------------------------------
# 为什么不直接用 MessageBoxW 默认 hWnd=0 的位置：
#   MessageBoxW 默认位置在不同 Windows 版本/用户 DPI/缩放比例上表现不一致：
#   有的机器上落在「屏幕正中央」、有的落在「任务栏旁但是错位」、
#   多显示器时甚至可能弹到非主屏上/跨屏夹在两屏中间。
#   通过 HCBT_ACTIVATE 钩子在激活瞬间拿到 HWND 强制 SetWindowPos，
#   可以精确地、跨版本、跨 DPI 地落在「主屏工作区右下角 + 固定 margin」。
#
# 为什么要装 WH_CBT 钩子而不是先 MessageBoxW 再 FindWindow：
#   FindWindow 靠标题/类名查找存在竞态和误匹配（相同标题已有其他窗口会搞错），
#   并且 FindWindow 需要轮询延迟一会儿才找得到，体验差。
#   HCBT 钩子在窗口激活第一时间就能拿到它的 HWND，准确且无竞态。
#
# 钩子必须装在"显示 MessageBoxW 的那根线程"上（我们调度到 GUI 线程，就装在 GUI 线程），
# HHOOK 是线程相关的，装钩子的线程和被拦截的线程必须是同一根，这里刚好匹配。
_HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_int,      # 返回值 LRESULT
    ctypes.c_int,      # nCode
    wintypes.WPARAM,   # wParam（HCBT_ACTIVATE 时是 HWND）
    wintypes.LPARAM,   # lParam
)
_position_hook_handle: Any = None
_position_hook_lock = threading.Lock()


def _get_primary_screen_bottom_right(width_px: int, height_px: int) -> tuple[int, int]:
    """计算主屏工作区右下角位置，返回 (x, y) 用于 SetWindowPos（窗口左上坐标）。

    为什么和 SettingsView 用同一套 margin：
    - SettingsView 固定位置：x=屏宽-窗宽-16, y=屏高-窗高-56
    - 为了整体视觉一致，所有对话框也用相同的右下角定位方式，
      和设置窗口「叠在同一块区域」，符合用户使用习惯。
    - 16 是右侧留白（不贴死屏幕右边，视觉更舒适）。
    - 56 是下方留白（留出任务栏高度 + 一点呼吸空间）；
      使用 SPI_GETWORKAREA 时已经排除了任务栏，所以 margin 可以更小，
      但为了保持和 SettingsView 的视觉位置一致，这里还是用 16/56。
    """
    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
    SPI_GETWORKAREA = 0x0030
    work = RECT()
    try:
        ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, ctypes.sizeof(work), ctypes.byref(work), 0)
        area_w = work.right - work.left
        area_h = work.bottom - work.top
    except Exception:
        # 兜底：按主屏分辨率（不排除任务栏）
        area_w = ctypes.windll.user32.GetSystemMetrics(0)   # SM_CXSCREEN
        area_h = ctypes.windll.user32.GetSystemMetrics(1)   # SM_CYSCREEN
        work = RECT()
        work.left = 0
        work.top = 0
        work.right = area_w
        work.bottom = area_h
    # 右下角：窗口右边 = 工作区右边 - 16；窗口下边 = 工作区下边 - 56
    x = work.right - width_px - 16
    y = work.bottom - height_px - 56
    # 极小窗口：防止 x/y 为负（理论上不会出现，防御性处理）
    x = max(work.left, x)
    y = max(work.top, y)
    return x, y


def _cbt_position_proc(nCode: int, wParam: int, lParam: int) -> int:
    """WH_CBT 钩子回调：在弹框激活时把它移到屏幕右下角（任务栏旁）。"""
    global _position_hook_handle
    try:
        if nCode == _HCBT_ACTIVATE and wParam:
            hwnd = wParam
            # 取当前窗口尺寸（使用 Client 会漏掉标题栏，用 GetWindowRect 取完整外框）
            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
            rc = RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rc))
            w = rc.right - rc.left
            h = rc.bottom - rc.top
            if w and h:
                x, y = _get_primary_screen_bottom_right(w, h)
                # SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE（保持尺寸/层级/不让自己抢激活态——HCBT 还在激活流程中，避免重入）
                SWP_NOSIZE = 0x0001
                SWP_NOZORDER = 0x0004
                SWP_NOACTIVATE = 0x0010
                ctypes.windll.user32.SetWindowPos(
                    hwnd, 0, x, y, 0, 0,
                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
                )
                logger.debug("dialogs._cbt_position_proc: 弹框定位(右下) hwnd=%s size=%dx%d pos=(%d,%d)", hwnd, w, h, x, y)
    except Exception as e:
        logger.debug("dialogs._cbt_position_proc: 钩子回调异常（不影响弹框显示）: %s", e)
    # 钩子正常返回：CallNextHookEx
    return ctypes.windll.user32.CallNextHookEx(_position_hook_handle, nCode, wParam, lParam)


_cbt_position_proc_ref = _HOOKPROC(_cbt_position_proc)  # 保存引用防止 GC 回收


def _install_position_hook() -> None:
    """在当前线程安装 WH_CBT 钩子（仅用于将弹框放到右下角）。"""
    global _position_hook_handle
    with _position_hook_lock:
        if _position_hook_handle is None:
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            tid = kernel32.GetCurrentThreadId()
            user32 = ctypes.windll.user32
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.SetWindowsHookExW.argtypes = (ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
            _position_hook_handle = user32.SetWindowsHookExW(
                _WH_CBT, _cbt_position_proc_ref, 0, tid,
            )
            logger.debug("dialogs._install_position_hook: 已安装 WH_CBT 钩子 handle=%s tid=%d", _position_hook_handle, tid)


def _uninstall_position_hook() -> None:
    """卸载 WH_CBT 钩子（弹框关闭后立即卸载，避免影响其他普通窗口）。"""
    global _position_hook_handle
    with _position_hook_lock:
        if _position_hook_handle is not None:
            try:
                ctypes.windll.user32.UnhookWindowsHookEx(_position_hook_handle)
                logger.debug("dialogs._uninstall_position_hook: 已卸载 WH_CBT 钩子 handle=%s", _position_hook_handle)
            finally:
                _position_hook_handle = None


# ---------------------------------------------------------------------------
# GUI 线程内部执行：真正的 MessageBoxW 调用（带右下角定位钩子 + 异常兜底）
# ---------------------------------------------------------------------------
def _msgbox_on_gui_thread(message: str, title: str, flags: int) -> int:
    """在当前 GUI 线程里调用 MessageBoxW（调用前装钩子，返回后卸载钩子）。

    为什么必须在 GUI 线程里调用：
    1. 带 Tk mainloop 的 GUI 线程有完整消息泵，MessageBoxW 的模态循环能正常退出，
       避免出现"点确定按钮没反应/无法关闭"的无响应现象。
    2. WH_CBT 钩子是线程级别的，装钩子的线程和 MessageBoxW 线程必须相同，
       这样 HCBT_ACTIVATE 才会真正被回调到，右下角定位才能生效。
    """
    _install_position_hook()
    try:
        # hWnd = 0：没有所有者窗口，使用桌面窗口
        # 为什么不用 Tk 根窗口的 hwnd：Tk 根窗口是隐藏的，作为 MessageBoxW 所有者
        # 在某些 Windows 版本上会让弹框也不可见或 z-order 异常。0 = 桌面窗口，最稳妥。
        try:
            ret = ctypes.windll.user32.MessageBoxW(0, message, title, flags)
        except Exception as e:
            logger.warning("dialogs._msgbox_on_gui_thread: MessageBoxW 异常: %s", e)
            ret = 0
        logger.debug("dialogs._msgbox_on_gui_thread: MessageBoxW 返回值=%d", ret)
        return int(ret or 0)
    finally:
        _uninstall_position_hook()


# ---------------------------------------------------------------------------
# 系统通知（线程安全，短提示用）
# ---------------------------------------------------------------------------
def show_notify(message: str, title: str = "SAU") -> None:
    """显示 Windows Toast 通知（线程安全，使用 pystray Icon.notify）。

    适用场景：短提示（1-2 行），不打断用户，几秒自动消失。
    不适用场景：账号列表/检查结果等多行内容 —— Windows Toast 有高度限制，
    超过 2 行会被截断，请使用 show_info 替代。
    """
    msg_preview = message[:50] + ("..." if len(message) > 50 else "")
    logger.debug("dialogs.show_notify: title=%s, message_preview=%s", title, msg_preview)
    try:
        if _icon_ref is not None:
            _icon_ref.notify(message, title)
        else:
            print(f"[{title}] {message}")
    except Exception:
        print(f"[{title}] {message}")


# ---------------------------------------------------------------------------
# 信息展示弹框（单行信息或多行列表；任意线程可调用，屏幕右下角）
# ---------------------------------------------------------------------------
def show_info(message: str, title: str = "SAU") -> None:
    """信息展示弹框（单确定按钮 + MB_ICONINFORMATION，屏幕右下角，多行无截断）。

    为什么统一调度到 GUI 线程：
    - 菜单项回调在 pystray 的内部线程执行，没有自己的消息泵，
      直接 MessageBoxW 在 Windows 10/11 上偶现"点击确定按钮没反应/无法关闭"
      的现象（pystray 底层消息循环和 MessageBoxW 模态循环互相干扰）。
    - 切到 GUI 线程（Tk mainloop 常驻）后，消息泵是完整的，弹框一定能正常关闭。

    为什么不继续用 pystray icon.notify：
    - Windows Toast 通知有高度/行数限制，通常只能完整显示 2 行，第 3 行起会被系统
      自动截断或完全不可见 → 账号状态/检查结果这种 3+ 行的内容会出现"扫到 3 个
      只显示 2 个"的视觉错觉。
    - MessageBoxW 由系统渲染，无行数限制，内容多时自动出现垂直滚动条，保证全部
      信息可见。

    为什么默认放在屏幕右下角（而不是屏幕中央）：
    - 托盘图标就位于屏幕右下角任务栏旁，用户的视觉焦点通常就在这一带，
      弹框落在右下角符合「靠近操作源头」的交互直觉，和 Settings 窗口保持一致。
    - 居中弹窗会在用户正在打字/阅读时突然挡住视线，对专注操作打扰更大，
      非阻塞性质的"确定"/"是/否"提示更适合在右下角温和出现。
    """
    msg_preview = message.replace("\n", " | ")[:120]
    logger.info("dialogs.show_info: 弹框展示信息 title=%s preview=%s", title, msg_preview)

    flags = _MB_OK | _MB_ICONINFORMATION | _MB_SYSTEMMODAL

    def _task(root: Any) -> None:
        # GUI 线程内执行
        try:
            _msgbox_on_gui_thread(message, title, flags)
        except Exception as e:
            logger.warning("dialogs.show_info: GUI 线程内 MessageBoxW 失败，回退 show_notify error=%s", e)
            try:
                show_notify(message, title)
            except Exception:
                pass

    try:
        gui_thread.schedule(_task)
    except Exception as e:
        # 最极端情况：GUI 线程未就绪/调度失败（启动早期）— 直接退回 Toast，不阻塞调用方
        logger.warning("dialogs.show_info: gui_thread.schedule 失败，回退 show_notify: %s", e)
        show_notify(message, title)


# ---------------------------------------------------------------------------
# 是/否确认弹框（任意线程可调用；屏幕右下角；返回布尔，等待用户点击）
# ---------------------------------------------------------------------------
def show_confirm(message: str, title: str = "确认") -> bool:
    """是/否确认对话框（任意线程可调用，屏幕右下角；点击是返回 True，否则 False）。

    为什么同样切 GUI 线程 + 用 Event 等待结果：
    - show_confirm 需要返回值，不像 show_info 可以"fire and forget"。
    - 调用方（任意线程）通过 Event 阻塞等待 GUI 线程里 MessageBoxW 返回。
    - Event.wait 有 10 分钟超时（基本不会撞），避免某种极端情况下死等。
    """
    logger.info("dialogs.show_confirm: 弹窗确认 title=%s", title)

    flags = _MB_YESNO | _MB_ICONQUESTION | _MB_SYSTEMMODAL
    result_holder: dict[str, int] = {"value": _IDNO}
    done = threading.Event()

    def _task(root: Any) -> None:
        # GUI 线程内执行，填结果并唤醒调用方
        try:
            ret = _msgbox_on_gui_thread(message, title, flags)
            result_holder["value"] = int(ret or _IDNO)
        except Exception as e:
            logger.warning("dialogs.show_confirm: GUI 线程内 MessageBoxW 异常: %s", e)
            result_holder["value"] = _IDNO
        finally:
            done.set()

    try:
        gui_thread.schedule(_task)
    except Exception as e:
        logger.warning("dialogs.show_confirm: gui_thread.schedule 失败，按取消处理: %s", e)
        return False

    # 10 分钟超时：用户长时间不点也不会卡死（实际上肯定会点）
    done.wait(timeout=600)
    result = result_holder["value"] == _IDYES
    logger.info("dialogs.show_confirm: 用户选择 result=%s (raw=%d)", result, result_holder["value"])
    return result
