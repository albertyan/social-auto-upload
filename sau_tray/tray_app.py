"""
sau_tray.tray_app
~~~~~~~~~~~~~~~~~
系统托盘应用程序入口（pystray 菜单编排 + 模块组装）。

MVC 架构：
- tray_app.py — 组合根（创建 Controller 实例，连接 View，构建菜单）
- controllers/ — 业务逻辑层
- views/ — 视图层
- services/ — 服务层
- core/ — 基础设施层
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
from pathlib import Path
from typing import Any

# ── 基础设施层（最早导入：崩溃捕获 + 路径修正 + 垫片） ──
from sau_tray.core import config as cfg
from sau_tray.core.config import (  # noqa: F401
    SAU_HOME, _APP_VERSION, _PROJECT_ROOT, global_except_hook,
)
from sau_tray.core import state, gui_thread
from sau_tray.services import machine_id, system_svc

# ── 视图层 & 控制层 ──
from sau_tray.views import dialogs
from sau_tray.controllers.settings_ctrl import SettingsController
from sau_tray.controllers.login_ctrl import LoginController
from sau_tray.controllers.service_ctrl import ServiceController

# 全局异常钩子（紧随 config 导入之后）
sys.excepthook = global_except_hook

# ── 兼容性别名 ──
_APP_NAME = cfg.APP_NAME
_PLATFORM_DISPLAY_NAMES = cfg.PLATFORM_DISPLAY_NAMES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 图标绘制（供初始图标使用）
# ---------------------------------------------------------------------------
def _create_icon_image(color: str) -> Any:
    """用 Pillow 绘制纯色圆形图标（32×32）。"""
    from PIL import Image, ImageDraw
    size = 32
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, size - 3, size - 3], fill=color, outline="white", width=1)
    return img


# ---------------------------------------------------------------------------
# Controller 实例（在 run_tray 中初始化）
# ---------------------------------------------------------------------------
_settings_ctrl: SettingsController | None = None
_login_ctrl: LoginController | None = None
_service_ctrl: ServiceController | None = None


# ---------------------------------------------------------------------------
# 菜单回调（仅事件转发到 Controller）
# ---------------------------------------------------------------------------
def _on_settings(icon: Any, item: Any) -> None:
    """设置窗口。"""
    _gt = gui_thread.get_thread()
    if _gt is None or not _gt.is_alive():
        dialogs.show_notify("设置功能暂不可用，请稍后重试")
        return
    _settings_ctrl.view.show()


def _on_exit_tray(icon: Any, item: Any) -> None:
    """退出：委托给 ServiceController（异步执行）。"""
    def _do_exit() -> None:
        _service_ctrl.on_exit(icon)
    threading.Thread(target=_do_exit, daemon=True, name="SAU-Exit").start()


# ---------------------------------------------------------------------------
# 构建菜单
# ---------------------------------------------------------------------------
def _make_login_action(key: str) -> Any:
    """为平台登录菜单项生成 2 参数回调。

    pystray 的 _assert_action 按 co_argcount 校验回调，只接受 0/1/2 个参数，
    不能用带默认参数的 3 参数 lambda（默认参数也计入 co_argcount）。
    """
    def _action(icon: Any, item: Any) -> None:
        _login_ctrl.login(key)
    return _action


def _build_menu() -> Any:
    """构建 pystray 菜单（所有回调转发到 Controller）。"""
    from pystray import MenuItem, Menu

    service_items = [
        MenuItem("启动服务", lambda icon, item: _service_ctrl.start_service()),
        MenuItem("停止服务", lambda icon, item: _service_ctrl.stop_service()),
        MenuItem("重启服务", lambda icon, item: _service_ctrl.restart_service()),
    ]

    login_items = [
        MenuItem(name, _make_login_action(key))
        for key, name in _PLATFORM_DISPLAY_NAMES.items()
    ]

    items = [
        MenuItem("服务", Menu(*service_items)),
        MenuItem("平台登录", Menu(*login_items)),
        MenuItem("账号状态", _service_ctrl.on_show_accounts),
        MenuItem("重新检查账号有效性", _service_ctrl.on_recheck_accounts),
        MenuItem("设置…", _on_settings),
        MenuItem("打开日志目录", _service_ctrl.on_open_logs),
        MenuItem("关于", _service_ctrl.on_about),
        MenuItem("检查更新", _service_ctrl.on_check_update),
        MenuItem("退出服务", _on_exit_tray),
    ]

    return Menu(*items)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def run_tray() -> None:
    """启动系统托盘应用。"""
    global _settings_ctrl, _login_ctrl, _service_ctrl

    from pystray import Icon

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("SAU Tray starting (SAU_HOME=%s)", SAU_HOME)

    # 后台预加载机器码
    threading.Thread(target=machine_id.preload, daemon=True, name="SAU-MachineCode-Preload").start()

    # 启动专用 GUI 线程
    gui_thread.start()

    # 创建初始图标（红色）
    initial_icon = _create_icon_image("#FF4444")

    # 创建 Controller 实例
    _settings_ctrl = SettingsController()
    _login_ctrl = LoginController()
    _service_ctrl = ServiceController()

    # 创建托盘图标
    icon = Icon(
        name=_APP_NAME,
        icon=initial_icon,
        title=f"{_APP_NAME}  ✕ 服务未运行",
        menu=_build_menu(),
    )

    # 注入图标引用
    dialogs.set_icon(icon)
    _service_ctrl.set_icon(icon)

    # 启动后台线程（状态轮询 + 图标更新）
    stop_event = _service_ctrl.start_background_threads()

    # 运行托盘（阻塞）
    try:
        icon.run()
    finally:
        stop_event.set()
        try:
            gui_thread.stop()
        except Exception:
            pass
        # 安全网：确保 SAU 进程被清理
        cleanup_lock = gui_thread.get_cleanup_lock()
        with cleanup_lock:
            if not gui_thread.is_cleanup_done():
                try:
                    system_svc.kill_sau_processes()
                except Exception:
                    pass
                system_svc.release_single_instance()
                gui_thread.set_cleanup_done()
        logger.info("SAU Tray stopped")


def main() -> None:
    """入口函数。"""
    if not system_svc.acquire_single_instance():
        try:
            ctypes.windll.user32.MessageBoxW(
                0, "SAU 服务已在运行中，请勿重复启动。", "SAU Agent", 0x40,
            )
        except Exception:
            pass
        sys.exit(0)

    try:
        try:
            run_tray()
        except Exception:
            try:
                import datetime
                import traceback
                exe_dir = Path(sys.executable if sys.executable.lower().endswith(".exe") else __file__).resolve().parent
                log_file = exe_dir / "sau-tray-crash.log"
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(f"\n[{ts}] === sau-tray.exe CRASH ===\n")
                    f.write(f"sys.executable = {sys.executable}\n")
                    f.write(f"sys.argv = {sys.argv}\n")
                    f.write(f"SAU_HOME = {SAU_HOME}\n")
                    f.write(traceback.format_exc())
            except Exception:
                pass
            raise
    finally:
        system_svc.release_single_instance()


if __name__ == "__main__":
    try:
        import datetime as _dt
        _exe_dir = Path(sys.executable if sys.executable.lower().endswith(".exe") else __file__).resolve().parent
        _log_file = _exe_dir / "sau-tray-crash.log"
        _ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_log_file, "a", encoding="utf-8") as _f:
            _f.write(f"[{_ts}] === sau-tray.exe started ===\n")
            _f.write(f"[{_ts}] sys.executable = {sys.executable}\n")
            _f.write(f"[{_ts}] sys.argv = {sys.argv}\n")
    except Exception:
        pass
    main()
