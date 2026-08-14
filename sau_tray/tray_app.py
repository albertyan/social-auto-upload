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
    """动态"启动服务"文本。

    按用户要求：
    - 服务处于运行状态 → 显示「已启动」（同时菜单项置灰，不可点击）
    - 服务未运行 → 显示「启动服务」（可用）
    """
    return "已启动" if _is_service_running() else "启动服务"


def _text_stop_service(item: Any) -> str:
    """动态"停止服务"文本。

    按用户要求：
    - 服务处于运行状态 → 显示「停止服务」（可用）
    - 服务未运行 → 显示「已停止」（同时菜单项置灰，不可点击）
    """
    return "停止服务" if _is_service_running() else "已停止"


def _enabled_start_service(item: Any) -> bool:
    """启动菜单项可用性：服务未运行时才允许点击。

    为什么这么设计：
        当服务已经 running 时再点"启动服务"只会发一条重复 start 命令给 SCM，
        SCM 会忽略重复 start 但会打日志。直接把菜单项置灰，
        可以让用户在 UI 上一眼就知道"当前这个操作不能再做"。
    """
    return not _is_service_running()


def _enabled_stop_service(item: Any) -> bool:
    """停止菜单项可用性：服务运行时才允许点击。"""
    return _is_service_running()


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
        # 启动菜单：running → 文本「已启动」+ 置灰；stopped → 文本「启动服务」+ 可用
        MenuItem(
            _text_start_service,
            lambda icon, item: _service_ctrl.start_service(),
            enabled=_enabled_start_service,
        ),
        # 停止菜单：running → 文本「停止服务」+ 可用；stopped → 文本「已停止」+ 置灰
        MenuItem(
            _text_stop_service,
            lambda icon, item: _service_ctrl.stop_service(),
            enabled=_enabled_stop_service,
        ),
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

    # 统一格式：控制台 + 文件日志都用同样的时间戳格式，
    # 与 runner.py / service_host.py / sau_agent.py 保持一致（方便跨模块 grep 日志）
    # 为什么显式写 datefmt：Python 默认 asctime 是 `2003-07-08 16:49:45,896`（逗号毫秒，各版本略有差异）
    # 显式 `%Y-%m-%d %H:%M:%S` 与其他入口完全对齐，排障时跨文件不会因为时间格式差异产生歧义。
    _COMMON_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    _COMMON_DATEFMT = "%Y-%m-%d %H:%M:%S"

    candidates = [SAU_HOME / "logs"]
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "SAU" / "logs")
    logger.info("文件日志候选目录: %s", [str(c) for c in candidates])  # 为什么打这条日志：记录日志目录选择顺序，排查日志文件找不到的问题

    last_err: Exception | None = None
    for log_dir in candidates:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                log_dir / "sau-tray.log",
                maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
            )
            # 文件日志必须独立配置 formatter：
            # RotatingFileHandler 默认继承 basicConfig 格式（理论上有 asctime），
            # 但某些 Python 版本/某些 Nuitka 打包后 logger handler 注册顺序会
            # 导致 fallback 到 root logger 的默认格式（只有 message 没有时间戳），
            # 这里显式 setFormatter 以保证文件里每行一定有「年-月-日 时:分:秒」时间戳。
            fh.setFormatter(logging.Formatter(_COMMON_FMT, datefmt=_COMMON_DATEFMT))
            logging.getLogger().addHandler(fh)
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

    # 统一日志格式：控制台 + 后续追加的文件日志都按同一个 fmt/datefmt，
    # 与 runner.py / service_host.py / sau_agent.py / sau_ops.py 保持一致。
    _COMMON_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    _COMMON_DATEFMT = "%Y-%m-%d %H:%M:%S"
    logging.basicConfig(
        level=logging.INFO,
        format=_COMMON_FMT,
        datefmt=_COMMON_DATEFMT,
        force=True,  # 为什么 force=True：Nuitka 打包或某些热重载场景下 root logger 之前已经被 basicConfig 初始化过；
                    # Python 3.8+ 默认不会覆盖已有 handler 的 formatter，会导致"明明写了 format 但日志还是没时间戳"
                    # 的诡异问题。force=True 强制重新配置 root logger，确保所有 handler 都用我们指定的格式。
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

    # ------------------------------------------------------------------
    # 启动 Banner：连通性 + 环境自检日志
    # （为什么放在这里：此时 Controller 已实例化、GUI 线程已启动、日志写入可用，
    #  打包安装后用户反馈"托盘一直未连接"时，只看这份 Banner 就能定位根因，
    #  不需要让用户再跑诊断脚本）
    # ------------------------------------------------------------------
    def _startup_self_check() -> None:
        """后台执行一次性自检，结果打到 info 日志（不阻塞托盘启动）。"""
        try:
            import urllib.request
            import urllib.error
            import socket
            from sau_service.service_host import get_service_status
            from sau_agent_pkg.config import load_local_token

            # 1) Windows SCM 层面服务状态（这是最权威的状态，比本地 API 准）
            try:
                scm_status = get_service_status()
            except Exception as se:
                scm_status = f"query_error:{type(se).__name__}:{se}"

            # 2) local_token.bin 是否存在 + 长度（500=token 未生成；401=token 不匹配）
            try:
                token = load_local_token() or ""
                token_len = len(token) if token else 0
                token_status = f"len={token_len}" if token_len else "EMPTY/MISSING"
            except Exception as te:
                token_len = -1
                token_status = f"read_error:{type(te).__name__}:{te}"

            # 3) LOCAL_API_URL 5410 端口是否真的在监听（=服务真的起来了）
            host = "127.0.0.1"
            port = 5410
            port_open = False
            try:
                with socket.create_connection((host, port), timeout=0.8):
                    port_open = True
            except Exception:
                port_open = False

            # 4) 连通性 HTTP 层检测（超时 1.5s，能直接区分是端口被占 / 鉴权错 / 服务初始化慢）
            http_status: str = "N/A"
            http_body_preview: str = ""
            try:
                token = load_local_token() or ""
                req = urllib.request.Request(_LOCAL_API_URL + "/status", method="GET")
                if token:
                    req.add_header("X-SAU-Local-Token", token)
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    http_status = str(resp.status)
                    raw = resp.read(256).decode(errors="replace")
                    http_body_preview = raw[:160].replace("\n", "\\n")
            except urllib.error.HTTPError as he:
                http_status = f"HTTP_{he.code}"
                try:
                    raw = he.read(256).decode(errors="replace")
                    http_body_preview = raw[:160].replace("\n", "\\n")
                except Exception:
                    http_body_preview = ""
            except urllib.error.URLError as ue:
                http_status = f"URL_ERR_{type(ue.reason).__name__}"
                http_body_preview = str(ue.reason)[:160]
            except Exception as he:
                http_status = f"UNKNOWN_ERR_{type(he).__name__}"
                http_body_preview = str(he)[:160]

            # 5) config.json 是否存在 + agent_id / server_url（空值=未绑定服务端=必然未连接）
            try:
                from sau_agent_pkg.config import load_config
                cfg = load_config() or {}
                has_agent_id = bool(cfg.get("agent_id"))
                has_server_url = bool(str(cfg.get("server_url", "")).strip())
                config_ok = f"agent_id={'Y' if has_agent_id else 'N'} server_url={'Y' if has_server_url else 'N'}"
            except Exception as ce:
                config_ok = f"load_error:{type(ce).__name__}:{ce}"

            # 为什么从 sau_tray.core.config 引入 LOCAL_API_URL：
            #   之前这里直接引用了 LOCAL_API_URL 标识符，但它在 tray_app.py 顶层未 import，
            #   会抛 NameError: name 'LOCAL_API_URL' is not defined，刚好被外层 except 吞掉
            #   只打 warning 不影响启动，但启动 Banner 自检日志就缺失了。
            try:
                from sau_tray.core.config import LOCAL_API_URL as _LOCAL_API_URL
            except Exception:
                _LOCAL_API_URL = "http://127.0.0.1:5410"

            logger.info(
                "========== 启动 Banner 自检 ==========\n"
                "  SAU_HOME            = %s\n"
                "  _LOCAL_API_URL       = %s (port_5410_listen=%s)\n"
                "  SCM_service_status  = %s\n"
                "  local_token_status  = %s (raw_token_len=%d)\n"
                "  HTTP_/status_check  = %s  body=%s\n"
                "  config_status       = %s\n"
                "  快速结论: %s\n"
                "=======================================",
                SAU_HOME,
                _LOCAL_API_URL, "YES" if port_open else "NO",
                scm_status,
                token_status, token_len,
                http_status, http_body_preview or "(empty)",
                config_ok,
                _self_check_summary(scm_status, port_open, token_len, http_status, config_ok),
            )
        except Exception as ce:
            logger.warning("启动 Banner 自检异常（不影响托盘启动，仅排障用）: %s", ce, exc_info=True)

    def _self_check_summary(
        scm_status: str,
        port_open: bool,
        token_len: int,
        http_status: str,
        config_ok: str,
    ) -> str:
        """把自检结果浓缩成一句话，帮用户快速理解为什么"托盘显示未连接"。"""
        if scm_status not in ("running", "starting"):
            return f"[关键] Windows 服务当前={scm_status}，未启动状态 → 先启动服务后再观察"
        if not port_open:
            return f"[关键] SCM={scm_status} 但 5410 未监听 → LocalApiServer 启动失败，查 sau-service.log 里 _main 入口是否崩溃"
        if token_len <= 0:
            return f"[关键] local_token 缺失/长度 0 → 服务 generate_local_token() 未生成，查 SAU_HOME 权限，要求 SYSTEM 用户有写权限"
        if http_status in ("500", "HTTP_500"):
            return f"[关键] HTTP_500(Local token missing) → 服务端没生成 token，需检查服务启动日志"
        if http_status in ("401", "HTTP_401"):
            return f"[关键] HTTP_401(Unauthorized) → 托盘读到的 token 与服务端生成的不匹配，重装服务后可能出现，重启托盘通常能解决"
        if "agent_id=N" in config_ok or "server_url=N" in config_ok:
            return f"[次要] config.json 缺失 agent_id/server_url → 即使本地 API 通，也无法连上 opcgeo 服务端 → 托盘菜单『设置…』里配置 Server URL + 绑定 Agent 后会正常"
        if http_status == "200":
            return f"[正常] 全部检查项通过，托盘稍后 3~6 秒内轮询到服务即可显示绿色/已连接"
        return f"[待观察] 服务正在启动或有异常，http_status={http_status}，稍后再看状态轮询 warning 日志"

    # 后台跑，不阻塞 icon.run（用户要立即看到托盘图标）
    threading.Thread(target=_startup_self_check, daemon=True, name="SAU-StartupSelfCheck").start()

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
