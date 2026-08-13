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
import os
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

# 退出幂等闸门：退出清理进行中时重复点击“退出服务”直接忽略
_exit_in_progress = threading.Event()


# ---------------------------------------------------------------------------
# 菜单回调（仅事件转发到 Controller）
# ---------------------------------------------------------------------------
def _on_settings(icon: Any, item: Any) -> None:
    """设置窗口。"""
    logger.info("用户点击设置菜单，准备打开设置窗口")  # 为什么打这条日志：追踪用户打开设置窗口的操作，排查设置窗口无法打开的问题
    _gt = gui_thread.get_thread()
    if _gt is None or not _gt.is_alive():
        logger.warning("GUI 线程未就绪，设置窗口无法打开")  # 为什么打这条日志：记录 GUI 线程异常导致设置窗口打开失败的场景
        dialogs.show_notify("设置功能暂不可用，请稍后重试")
        return
    _settings_ctrl.view.show()


def _on_exit_tray(icon: Any, item: Any) -> None:
    """退出：委托给 ServiceController（异步执行，重复点击忽略）。"""
    logger.info("用户点击退出服务菜单")  # 为什么打这条日志：追踪用户主动触发退出的操作，排查退出流程问题
    if _exit_in_progress.is_set():
        logger.info("退出清理已在进行中，忽略重复点击")
        return
    _exit_in_progress.set()
    logger.info("启动异步退出清理线程 SAU-Exit")  # 为什么打这条日志：确认退出线程已启动，排查退出流程卡住的问题

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
        logger.info("用户点击平台登录菜单: platform=%s", key)  # 为什么打这条日志：追踪用户点击的平台登录菜单项，排查登录触发问题
        _login_ctrl.login(key)
    return _action


def _is_service_running() -> bool:
    """判断服务是否在运行（读 state 缓存，不阻塞 UI）。

    为什么不用 get_service_status() 查 SCM：
    - SCM 查询可能耗时几百毫秒，用户右键打开菜单时会卡一下
    - state 由后台轮询线程每 3s 更新一次，用于菜单项标识已经足够及时
    """
    status = state.get_status()
    return bool(status.get("service_running"))


def _text_start_service(item: Any) -> str:
    """动态"启动服务"文本：服务未运行时前面加 ● 标识。

    为什么要加标识：
    - 用户右键菜单时能一眼看出当前服务状态，不用点开子菜单再判断
    - 未运行用 ● 强调"可以执行的操作"
    """
    return "● 启动服务" if not _is_service_running() else "启动服务"


def _text_stop_service(item: Any) -> str:
    """动态"停止服务"文本：服务运行时前面加 ● 标识。

    为什么和启动服务互补：
    - 启动/停止两个菜单项里必有且仅有一个带标识，不会都有或都没有
    - 用户通过标识位置可以快速知道当前服务是"已停"还是"在跑"
    """
    return "● 停止服务" if _is_service_running() else "停止服务"


# 平台登录状态的 scan 结果缓存（3s TTL）
# 为什么加缓存：用户右键打开托盘菜单时，pystray 会对每个 MenuItem 的 callable 文本逐一重新求值，
# 7 个平台意味着一次右键要调 7 次 accounts.scan()，每次都扫 cookies 目录+SQLite+
# 会带来明显 I/O，菜单卡顿。缓存 TTL 设为 3 秒，和 service_running 的轮询粒度一致，
# 足够及时反映"刚登录完一个账号就打开菜单"的场景。
_LOGGED_SCAN_TTL_SEC = 3.0
_logged_scan_lock = threading.Lock()
_logged_scan_cache: set[str] | None = None
_logged_scan_ts: float = 0.0


def _get_cached_logged_platforms() -> set[str]:
    """返回「当前已登录账号的平台 key 集合（带缓存）。"""
    global _logged_scan_cache, _logged_scan_ts
    import time
    now = time.monotonic()
    with _logged_scan_lock:
        if _logged_scan_cache is not None and (now - _logged_scan_ts) < _LOGGED_SCAN_TTL_SEC:
            return _logged_scan_cache
    # 缓存未命中，真正扫一次
    try:
        from sau_agent_pkg import accounts
        accs = accounts.scan()
        keys: set[str] = {a["platform_key"] for a in accs}
    except Exception as e:
        logger.debug("_get_cached_logged_platforms: scan 失败，按空集合处理（不影响菜单显示）: %s", e)
        keys = set()
    with _logged_scan_lock:
        _logged_scan_cache = keys
        _logged_scan_ts = now
    return keys


def _make_text_platform_login(platform_key: str, display_name: str) -> Any:
    """为每个平台登录菜单项生成一个 callable 文本函数。

    pystray 每次显示菜单都会调用该函数，根据 scan 结果判断该平台下
    是否至少有一个已登录账号。有则在前面加 ● 标识。
    """
    display_name_ref = display_name
    platform_key_ref = platform_key

    def _text(item: Any) -> str:
        logged_keys = _get_cached_logged_platforms()
        return f"● {display_name_ref}" if platform_key_ref in logged_keys else display_name_ref
    return _text


def _build_menu() -> Any:
    """构建 pystray 菜单（所有回调转发到 Controller）。"""
    logger.info("开始构建系统托盘菜单，支持平台数: %d", len(_PLATFORM_DISPLAY_NAMES))  # 为什么打这条日志：记录菜单构建过程，确认支持的平台列表
    from pystray import MenuItem, Menu

    service_items = [
        # text 传 callable 而不是 str：pystray 每次显示菜单都会重新调用，拿到最新状态标识
        MenuItem(_text_start_service, lambda icon, item: _service_ctrl.start_service()),
        MenuItem(_text_stop_service, lambda icon, item: _service_ctrl.stop_service()),
        MenuItem("重启服务", lambda icon, item: _service_ctrl.restart_service()),
    ]

    login_items = [
        # 菜单文本 Callable：每次打开菜单重新生成，带缓存（3s）后重新扫一次
        MenuItem(
            _make_text_platform_login(key, name), _make_login_action(key)
        )
        for key, name in _PLATFORM_DISPLAY_NAMES.items()
    ]
    logger.info("平台登录菜单项构建完成: %s", list(_PLATFORM_DISPLAY_NAMES.keys()))  # 为什么打这条日志：确认平台登录菜单项的具体内容

    items = [
        MenuItem("服务", Menu(*service_items)),
        MenuItem("平台登录", Menu(*login_items)),
        MenuItem("账号状态", _service_ctrl.on_show_accounts),
        MenuItem("检查账号有效性", _service_ctrl.on_recheck_accounts),
        MenuItem("设置…", _on_settings),
        MenuItem("打开日志目录", _service_ctrl.on_open_logs),
        MenuItem("关于", _service_ctrl.on_about),
        MenuItem("检查更新", _service_ctrl.on_check_update),
        MenuItem("退出服务", _on_exit_tray),
    ]
    logger.info("系统托盘菜单构建完成，共 %d 个顶层菜单项", len(items))  # 为什么打这条日志：确认菜单构建成功

    return Menu(*items)


# ---------------------------------------------------------------------------
# 文件日志（带轮转 + 权限回退）
# ---------------------------------------------------------------------------
def _setup_file_logging() -> None:
    r"""为 root logger 追加 RotatingFileHandler（5MB × 3 份）。

    首选 SAU_HOME\logs；%ProgramData%\SAU\logs 普通用户可能无写
    权限，失败则回退 %LOCALAPPDATA%\SAU\logs；两级都失败时把告警
    追加写到 exe 目录 sau-tray-crash.log（不阻断启动）。
    """
    from logging.handlers import RotatingFileHandler

    candidates = [SAU_HOME / "logs"]
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "SAU" / "logs")
    logger.info("文件日志候选目录: %s", [str(c) for c in candidates])  # 为什么打这条日志：记录日志目录选择顺序，排查日志文件找不到的问题

    last_err: Exception | None = None
    for log_dir in candidates:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            logging.getLogger().addHandler(
                RotatingFileHandler(
                    log_dir / "sau-tray.log",
                    maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
                )
            )
            logger.info("已选定文件日志目录并创建 RotatingFileHandler: %s", log_dir)  # 为什么打这条日志：确认最终使用的日志目录位置
            return
        except Exception as e:
            last_err = e
            logger.error("无法创建文件日志目录 %s: %s", log_dir, e)  # 为什么打这条日志：记录每个候选目录失败的详细原因，排查权限问题

    # 两级都失败：把告警追加写到 exe 目录 sau-tray-crash.log
    try:
        import datetime
        exe_dir = Path(sys.executable if sys.executable.lower().endswith(".exe") else __file__).resolve().parent
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(exe_dir / "sau-tray-crash.log", "a", encoding="utf-8") as f:
            f.write(f"[{ts}] 警告: 文件日志创建失败（SAU_HOME 与 LOCALAPPDATA 均不可写）: {last_err}\n")
        logger.info("两级文件日志目录均失败，已写入 crash.log 告警: %s", exe_dir / "sau-tray-crash.log")  # 为什么打这条日志：确认 crash.log 告警写入成功
    except Exception as e:
        logger.error("两级文件日志目录均失败，且写入 crash.log 也失败: %s", e)  # 为什么打这条日志：极端情况下记录 crash.log 写入失败的错误


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
    # 生产环境 --windows-console-mode=disable 导致 stderr 丢失，
    # 必须追加文件日志供排障取证。首选 SAU_HOME\logs；普通用户
    # 可能无写权限时回退 %LOCALAPPDATA%\SAU\logs；两级都失败时
    # 把告警追加写到 exe 目录 sau-tray-crash.log。
    _setup_file_logging()
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    lock_ok = system_svc.acquire_single_instance()
    logger.info("SAU Tray starting: SAU_HOME=%s, python_version=%s, single_instance_lock=%s",  # 为什么打这条日志：启动关键信息快照，排查环境与单实例问题
                SAU_HOME, py_ver, lock_ok)

    # 后台预加载机器码
    threading.Thread(target=machine_id.preload, daemon=True, name="SAU-MachineCode-Preload").start()

    # 启动专用 GUI 线程
    logger.info("启动专用 GUI 线程 SAU-GUI-Thread")  # 为什么打这条日志：确认 GUI 线程启动时机，排查 GUI 无响应问题
    gui_thread.start()

    # 创建初始图标（红色）
    initial_icon = _create_icon_image("#FF4444")

    # 创建 Controller 实例
    logger.info("开始实例化 Controller: SettingsController, LoginController, ServiceController")  # 为什么打这条日志：追踪 Controller 初始化过程，排查构造异常
    _settings_ctrl = SettingsController()
    logger.info("SettingsController 实例化完成")  # 为什么打这条日志：确认 SettingsController 构造成功
    _login_ctrl = LoginController()
    logger.info("LoginController 实例化完成")  # 为什么打这条日志：确认 LoginController 构造成功
    _service_ctrl = ServiceController()
    logger.info("ServiceController 实例化完成")  # 为什么打这条日志：确认 ServiceController 构造成功

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
    except Exception as e:
        logger.warning("icon.run 异常退出: %s", e)  # 为什么打这条日志：记录托盘图标的异常退出，排查 icon 相关崩溃
    finally:
        logger.info("run_tray 清理阶段: 设置 stop_event")  # 为什么打这条日志：追踪清理步骤顺序，排查退出卡住
        stop_event.set()
        logger.info("run_tray 清理阶段: 调用 gui_thread.stop()")  # 为什么打这条日志：确认 GUI 线程停止步骤已执行
        try:
            gui_thread.stop()
        except Exception:
            logger.exception("run_tray: 停止 GUI 线程异常")
        # 安全网：确保 SAU 进程被清理
        logger.info("run_tray 清理阶段: 获取 cleanup_lock 并检查是否已清理")  # 为什么打这条日志：记录进程清理互斥锁获取时机
        cleanup_lock = gui_thread.get_cleanup_lock()
        with cleanup_lock:
            if not gui_thread.is_cleanup_done():
                logger.info("run_tray 清理阶段: 执行 kill_sau_processes() 清理残留进程")  # 为什么打这条日志：确认进程清理动作已触发
                try:
                    system_svc.kill_sau_processes()
                except Exception:
                    logger.exception("run_tray: 兜底进程清理异常")
                logger.info("run_tray 清理阶段: 执行 release_single_instance() 释放单实例锁")  # 为什么打这条日志：确认单实例锁释放步骤
                system_svc.release_single_instance()
                gui_thread.set_cleanup_done()
        logger.info("SAU Tray stopped")


def main() -> None:
    """入口函数。"""
    if not system_svc.acquire_single_instance():
        logger.info("单实例互斥锁获取失败，检测到已有实例运行，准备退出")  # 为什么打这条日志：记录重复启动被拦截的情况
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
        except Exception as e:
            import traceback as _tb
            tb_str = _tb.format_exc()
            logger.error("run_tray 运行异常，类型=%s: %s\n%s", type(e).__name__, e, tb_str)  # 为什么打这条日志：完整记录运行时异常与调用栈，排查崩溃原因
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
                logger.info("崩溃信息已写入 crash.log: %s", log_file)  # 为什么打这条日志：确认 crash.log 写入成功
            except Exception as ce:
                logger.error("写入 crash.log 失败: %s", ce)  # 为什么打这条日志：极端情况下记录 crash.log 写入失败
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
