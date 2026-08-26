# -*- coding: utf-8 -*-
"""瘦托盘（实施计划 S5；设计文档第 5 章：三菜单、无启停、轮询、Mutex）。

设计要点（v1.2 定案）：
- **三菜单**：① 打开控制台（浏览器访问 ``http://127.0.0.1:{port}/ui/t/<票据>``；
  托盘先持 ``X-SAU-Local-Token`` 调 ``POST /ui-ticket`` 换一次性票据，
  浏览器以票据换 Cookie 会话，§6.3；票据获取失败时回退打开 ``/ui/``
  根路径并记日志）；
  ② 打开日志目录（``%ProgramData%\\SAU\\logs``，排障入口）；③ 退出托盘
  （仅退出托盘进程，不影响服务）。**无启停菜单**——服务只靠延迟自启 +
  故障自动重启恢复（§4.2 / 第 5 章编者注）；
- **状态轮询**：每 ``POLL_INTERVAL`` 秒（默认 5，可配）调 ``GET /status``
  （``X-SAU-Local-Token`` 读 ``local_token.bin``，§3.7 契约：托盘→服务
  唯一通道）；按 ``ws_connected``/可达性切换图标（在线绿/离线灰，Pillow
  代码生成，无图片资源文件）与 tooltip；
- **离线提示**：服务不可达或 ``suspended`` 时气泡提示「服务未运行，系统会
  自动恢复」（§5.2 定案措辞，一字一致；挂起态走同一提示）；恢复在线时
  再提示一次；
- **单实例**：命名互斥量 ``SAUTrayMutex``（§5.3；会话本地命名——不加
  ``Global\\`` 前缀，标准用户无 SeCreateGlobalPrivilege 无法创建全局命名空
  间对象；托盘本就每用户会话一个，无需跨会话），已存在直接退出；
- **权限**：托盘以普通用户运行——仅读 ``local_token.bin``（users 可读）与写
  ``%ProgramData%\\SAU\\logs``（users-full），全程无提权操作。

可测试性：状态轮询/分类/气泡触发/图标生成/互斥量均为纯函数或独立原语，
``tests/verify_s5.py`` 模块级覆盖；``run()`` 为交互式入口（手动验证）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
import webbrowser

from sau_wrap import paths
from sau_wrap.agent import config as agent_config
from sau_wrap.logutil import setup_logger
from sau_wrap.version import APP_VERSION

#: 互斥量名（§5.3）。会话本地命名（不带 ``Global\\`` 前缀）：全局命名空间需
#: SeCreateGlobalPrivilege，标准用户会话下创建会被拒；托盘每用户会话一个，
#: 无需跨会话可见。
MUTEX_NAME = "SAUTrayMutex"

#: 托盘优雅退出命名事件（任务板 Task #2：卸载/升级不再 taskkill 强杀，
#: 而是 ``SetEvent`` 通知托盘走 ``icon.stop()``——发 NIM_DELETE，防通知区
#: 残留幽灵图标）。命名口径与 ``MUTEX_NAME`` 一致（见上方注释）：会话本地
#: 命名，不加 ``Global\\`` 前缀（标准用户无 SeCreateGlobalPrivilege；托盘
#: 每用户会话一个，卸载/升级器与托盘同会话，无需跨会话可见）。
EXIT_EVENT_NAME = "SAUTrayExitEvent"

#: 状态轮询间隔（秒，§5.2：5~10s，取 5；环境变量可覆盖，测试用）
POLL_INTERVAL = float(os.environ.get("SAU_TRAY_POLL_SECONDS", "5"))

#: HTTP 轮询超时（秒）
POLL_TIMEOUT = 3.0

#: 本地 API 默认端口（§4.4；config.json 的 local_api_port 可覆盖）
DEFAULT_PORT = 5409

#: 图标颜色（RGB）：在线绿 / 离线灰
COLOR_ONLINE = (46, 204, 113)
COLOR_OFFLINE = (149, 165, 166)

#: 气泡提示文案（§5.2 定案措辞）
NOTIFY_TITLE = "SAU Agent"
NOTIFY_DOWN = "服务未运行，系统会自动恢复"
NOTIFY_UP = "服务已恢复在线"
#: 凭证异常离线气泡（评审问题 3）：服务在运行、根因是凭证时，
#: 「系统会自动恢复」会误导用户，改用与 tooltip 措辞风格一致的处理指引。
NOTIFY_TOKEN = "凭证异常，请打开控制台处理"

#: 状态枚举（轮询结果分类）
ST_ONLINE = "online"          # /status 200 且 ws_connected=True
ST_OFFLINE = "offline"        # /status 200 但连接态异常（未连接/挂起）
ST_AUTH = "auth_error"        # 401（令牌不匹配，通常服务刚重启令牌已换）
ST_UNREACHABLE = "unreachable"  # 连接失败（服务未运行）

#: 异常态集合（图标转灰 + 触发离线提示）
_BAD_STATES = frozenset({ST_OFFLINE, ST_AUTH, ST_UNREACHABLE})


# ---------------------------------------------------------------- 纯逻辑（可测）


def resolve_local_port() -> int:
    """本地 API 端口：config.json 的 ``local_api_port``，缺省 5409（§4.4）。"""
    cfg = agent_config.load_config()
    return cfg.local_api_port if cfg else DEFAULT_PORT


def load_local_token() -> str | None:
    """读取 ``local_token.bin``（users 可读，§3.6/§3.7）；不存在/不可读返回 None。"""
    try:
        return paths.LOCAL_TOKEN_FILE.read_bytes().decode("utf-8").strip() or None
    except OSError:
        return None


def build_console_url(port: int) -> str:
    """控制台回退 URL（``/ui/`` 根路径；票据链路失败时的降级入口，§6.3）。"""
    return f"http://127.0.0.1:{port}/ui/"


def request_ui_ticket(port: int, token: str | None,
                      timeout: float = POLL_TIMEOUT) -> str | None:
    """``POST /ui-ticket`` 换一次性票据（§6.3 鉴权链路第一步）。

    托盘持 ``X-SAU-Local-Token`` 调用；失败（服务不可达/401/异常）返回 None，
    由调用方回退打开 ``/ui/``。
    """
    url = f"http://127.0.0.1:{port}/ui-ticket"
    req = urllib.request.Request(url, method="POST",
                                 headers={"X-SAU-Local-Token": token or ""})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    ticket = body.get("ticket")
    return ticket if isinstance(ticket, str) and ticket else None


def build_ticket_url(port: int, ticket: str) -> str:
    """票据核销入口 URL（§6.3：浏览器 GET 后种会话 Cookie 并 302 到 /ui/）。"""
    return f"http://127.0.0.1:{port}/ui/t/{ticket}"


#: token_status 异常值 → tooltip 连接态文案（与网络断开的「未连接」区分：
#: 网络正常但凭证有问题，须引导用户重新绑定/打开控制台处理）
_TOKEN_STATUS_TEXT = {
    "unbound": "未绑定（请打开控制台绑定）",
    "expired": "凭证已过期（请重新绑定）",
    "suspended": "已挂起（凭证，请打开控制台处理）",
}


def build_tooltip(state: str, body: dict | None) -> str:
    """tooltip：版本 / 连接态 / 活跃任务数。

    凭证态收紧（Task #2）：200 响应中 ``token_status`` 非 ok 时，连接态文案
    体现凭证问题（未绑定/过期/挂起）并给出处理指引，与网络断开（「未连接」）
    区分；字段缺失按 ok 处理（兼容旧版服务响应）。
    """
    if state == ST_UNREACHABLE:
        return f"SAU Agent v{APP_VERSION} | 服务不可达"
    if state == ST_AUTH:
        return f"SAU Agent v{APP_VERSION} | 令牌不匹配（服务可能刚重启）"
    if not body:
        return f"SAU Agent v{APP_VERSION}"
    connected = "在线" if body.get("ws_connected") else "未连接"
    if body.get("suspended"):
        connected = "已挂起"
    # token_status 非 ok：凭证问题优先于连接态展示（缺失按 ok 处理）
    token_status = body.get("token_status", "ok")
    if token_status != "ok":
        connected = _TOKEN_STATUS_TEXT.get(token_status, f"凭证异常（{token_status}）")
    return (
        f"SAU Agent v{body.get('version', APP_VERSION)} | "
        f"连接: {connected} | 活跃任务: {body.get('active_tasks', 0)}"
    )


def classify_http(status_code: int, body: dict | None) -> str:
    """HTTP 结果分类（四态：在线 / 离线 / 401 / 不可达由调用方单独给出）。

    判绿收紧（Task #2）：在线除 ``ws_connected`` 外还要求 ``token_status``
    为 ok——字段缺失按 "ok" 处理（向后兼容旧版服务响应）；
    expired/suspended/unbound 等归离线（图标转灰 + 离线提示）。
    """
    if status_code == 401:
        return ST_AUTH
    if (status_code == 200 and body and body.get("ws_connected")
            and not body.get("suspended")
            and body.get("token_status", "ok") == "ok"):
        return ST_ONLINE
    return ST_OFFLINE


def fetch_status(port: int, token: str | None, timeout: float = POLL_TIMEOUT) -> tuple[str, dict | None]:
    """轮询 ``GET /status``。返回 ``(状态, 响应体或 None)``。

    - 200 → ``classify_http`` 细分在线/离线；
    - 401 → ``auth_error``；其余 HTTP 错误 → ``offline``；
    - 连接失败（服务未运行）→ ``unreachable``。
    """
    url = f"http://127.0.0.1:{port}/status"
    req = urllib.request.Request(url, headers={"X-SAU-Local-Token": token or ""})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try:
                body = json.loads(resp.read().decode("utf-8"))
            except ValueError:
                body = None
            # 修复 3：响应体非 dict（如 JSON 数组/标量）时置 None，
            # 防 body.get 抛 AttributeError 逃出下方捕获清单
            if not isinstance(body, dict):
                body = None
            return classify_http(resp.status, body), body
    except urllib.error.HTTPError as exc:
        return (ST_AUTH if exc.code == 401 else ST_OFFLINE), None
    except (urllib.error.URLError, OSError, ValueError):
        return ST_UNREACHABLE, None


class StateTracker:
    """状态翻转追踪：仅在跨越「正常↔异常」边界时给出气泡提示（避免轮询噪音）。

    - 正常 → 异常：返回离线提示（§5.2 定案措辞）；
    - 异常 → 正常：返回恢复提示一次；
    - 同态重复：不提示。
    """

    def __init__(self) -> None:
        self.state: str | None = None

    def apply(self, new_state: str) -> str | None:
        """返回应弹出的气泡文案（无需提示返回 None）。"""
        prev, self.state = self.state, new_state
        if prev is None:
            # 首轮不弹提示（托盘刚启动，避免开机启动风暴期的打扰）
            return None
        was_bad = prev in _BAD_STATES
        is_bad = new_state in _BAD_STATES
        if not was_bad and is_bad:
            return NOTIFY_DOWN  # 含挂起态：§5.2 定案措辞同一提示
        if was_bad and not is_bad:
            return NOTIFY_UP
        return None


def pick_notice(state: str, body: dict | None,
                default_down: str = NOTIFY_DOWN,
                default_up: str = NOTIFY_UP) -> str:
    """气泡文案选择（评审问题 3，可测纯逻辑）。

    StateTracker 负责「是否弹」（边沿触发），本函数负责「弹什么」：
    - ``ST_OFFLINE`` 且 ``token_status`` 非 ok → 凭证文案（此时服务在运行、
      根因是凭证，用户等不到「自动恢复」，须引导打开控制台处理）；
    - 其余异常态 → 维持 §5.2 定案离线措辞（``default_down``，一字不动）；
    - 恢复在线 → 维持定案恢复措辞（``default_up``，一字不动）。
    """
    if (state == ST_OFFLINE and body
            and body.get("token_status", "ok") != "ok"):
        return NOTIFY_TOKEN
    if state in _BAD_STATES:
        return default_down
    return default_up


def make_icon_image(color: tuple[int, int, int], size: int = 64):
    """Pillow 代码生成简单色块图标（圆形），不引入图片资源文件。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(2, size // 16)
    draw.ellipse([pad, pad, size - pad, size - pad], fill=color + (255,))
    return img


def pick_icon_color(state: str) -> tuple[int, int, int]:
    """状态 → 图标颜色（在线绿 / 其余异常态灰）。"""
    return COLOR_ONLINE if state == ST_ONLINE else COLOR_OFFLINE


def signal_tray_exit(timeout: float = 8.0,
                     event_name: str = EXIT_EVENT_NAME,
                     mutex_name: str = MUTEX_NAME,
                     poll_interval: float = 0.2) -> int:
    """请求托盘优雅退出并等待其进程结束（安装器/升级器调用，可测纯逻辑）。

    链路：``SetEvent`` 命名事件 → 托盘轮询线程收到后走 ``icon.stop()``
    （发 NIM_DELETE，防通知区幽灵图标）→ 托盘进程退出，命名互斥量随之消失。
    本函数以「互斥量是否仍可打开」作为托盘存活判据轮询确认。

    返回值（进程退出码）：
    - 0 = 无需兜底：事件不存在（托盘未运行或旧版托盘无此事件）或托盘已退出；
    - 非 0 = 需调用方 taskkill 兜底：超过 ``timeout`` 托盘仍存在，或发生异常。
      （异常**不得**静默返回 0 伪成功，否则升级器会误以为托盘已优雅退出。）
    语义收紧（修复 3）：OpenEvent/OpenMutex 失败严格区分「对象不存在」
    （``pywintypes.error`` 且 ``winerror == 2``，即 ERROR_FILE_NOT_FOUND）与
    其余异常（如访问拒绝）——前者按原语义返回 0（事件不存在 = 托盘未运行，
    互斥量不存在 = 托盘已退出）；后者返回非 0，由调用方 taskkill 兜底。
    """
    import time
    import win32api
    import win32con
    import win32event

    import pywintypes

    def _is_object_missing(exc: Exception) -> bool:
        """命名对象不存在（winerror=2）判定：仅此情形按原语义放行返回 0。"""
        return isinstance(exc, pywintypes.error) and exc.winerror == 2

    try:
        handle = win32event.OpenEvent(win32con.EVENT_MODIFY_STATE, False, event_name)
    except Exception as exc:  # noqa: BLE001
        if _is_object_missing(exc):
            return 0  # 事件不存在：托盘未运行/旧版托盘，无需兜底（原语义）
        return 1  # 其余异常（如访问拒绝）：不得伪成功，调用方 taskkill 兜底
    if not handle:
        return 0
    try:
        try:
            win32event.SetEvent(handle)
        finally:
            win32api.CloseHandle(handle)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                mutex = win32event.OpenMutex(win32con.SYNCHRONIZE, False, mutex_name)
            except Exception as exc:  # noqa: BLE001
                if _is_object_missing(exc):
                    return 0  # 互斥量已随托盘进程退出而消失 → 视为已退出（原语义）
                return 1  # 其余异常（如访问拒绝）：不得伪成功，调用方兜底
            if not mutex:
                return 0
            win32api.CloseHandle(mutex)  # 打开成功仅用于探活，立即释放
            time.sleep(poll_interval)
        return 1  # 超时托盘仍存在 → 调用方 taskkill 兜底
    except Exception:  # noqa: BLE001 其余异常（如 SetEvent 被拒）：返回非 0，不伪成功
        return 1


def acquire_mutex(name: str = MUTEX_NAME):
    """创建命名互斥量（§5.3）。

    返回值语义（严格区分「已存在」与「创建失败」）：
    - 已存在（ERROR_ALREADY_EXISTS=183）→ 返回 ``None``，调用方应直接退出；
    - 创建失败（句柄为空且非 183，如权限拒绝）→ 抛 ``RuntimeError``，
      **绝不**误判为「已有实例在运行」；
    - 成功 → 返回互斥量句柄（调用方负责 CloseHandle）。
    """
    import win32api
    import win32event

    handle = win32event.CreateMutex(None, False, name)
    err = win32api.GetLastError()
    if err == 183:  # ERROR_ALREADY_EXISTS：已有托盘实例
        if handle:
            import contextlib

            with contextlib.suppress(Exception):
                win32api.CloseHandle(handle)  # 系统仍返回句柄，此处不持有
        return None
    if not handle:
        raise RuntimeError(
            f"创建互斥量 {name!r} 失败（GetLastError={err}）——非「已有实例」，"
            "请检查用户会话权限"
        )
    return handle


def open_console(port: int, logger: logging.Logger) -> None:
    """菜单①：打开控制台（§6.3 完整链路，无需提权）。

    先 ``POST /ui-ticket`` 换一次性票据，再打开 ``/ui/t/<票据>``（浏览器换会话）；
    票据获取失败（服务不可达等）时回退打开 ``/ui/`` 根路径并记日志（前端会展示
    401 引导页）。
    """
    token = load_local_token()
    ticket = request_ui_ticket(port, token)
    if ticket:
        url = build_ticket_url(port, ticket)
        logger.info("打开控制台（一次性票据链路）: %s", url)
    else:
        url = build_console_url(port)
        logger.warning("获取控制台票据失败（服务不可达或令牌不匹配），"
                       "回退打开控制台根路径: %s", url)
    webbrowser.open(url)  # Windows 下即 ShellExecute 默认浏览器


def open_logs_dir(logger: logging.Logger) -> None:
    """菜单②：打开日志目录（不存在则创建；排障入口，§5.1）。"""
    paths.ensure_logs_dir()
    logger.info("打开日志目录: %s", paths.LOGS_DIR)
    os.startfile(str(paths.LOGS_DIR))  # noqa: S606（ShellExecute 语义，无提权）


# ---------------------------------------------------------------- 托盘主体


def run() -> int:
    """托盘交互入口（``sau tray``）。阻塞直至退出菜单；返回退出码。

    崩溃隔离：托盘自身异常全部捕获并记 ``tray.log``（§14.1：5MB×3），
    与服务进程架构上天然隔离，托盘崩溃不影响服务。
    """
    import pystray
    from pystray import Menu, MenuItem

    logger = setup_logger("sau.tray", paths.TRAY_LOG_FILE, also_console=True)
    logger.info("托盘启动: version=%s pid=%d", APP_VERSION, os.getpid())

    # 单实例（§5.3）：已存在 → 退出；创建失败（非 183）→ 记日志报错退出，
    # 不误报「已在运行」
    try:
        mutex = acquire_mutex()
    except RuntimeError as exc:
        logger.error("互斥量创建失败，托盘无法启动（非单实例冲突）: %s", exc)
        print(f"SAU 托盘启动失败：{exc}")
        return 1
    if mutex is None:
        logger.info("检测到已有托盘实例（互斥量 %s 已存在），本次直接退出", MUTEX_NAME)
        print("SAU 托盘已在运行（单实例，见 %s）。" % paths.TRAY_LOG_FILE)
        return 0

    # 优雅退出命名事件（Task #2）：卸载/升级器经 ``sau tray-exit`` SetEvent
    # 通知托盘走 icon.stop()（发 NIM_DELETE，防幽灵图标），替代 taskkill 强杀。
    # 创建失败仅记日志不阻断（降级为仅支持菜单退出）。
    exit_event_handle = None
    try:
        import win32event

        exit_event_handle = win32event.CreateEvent(None, True, False, EXIT_EVENT_NAME)
        if not exit_event_handle:
            logger.warning("创建退出事件 %s 失败，降级为仅支持菜单退出", EXIT_EVENT_NAME)
    except Exception:  # noqa: BLE001
        logger.warning("创建退出事件 %s 异常，降级为仅支持菜单退出",
                       EXIT_EVENT_NAME, exc_info=True)

    port = resolve_local_port()
    tracker = StateTracker()
    icon_holder: dict = {}

    # 图标预缓存（Task #2）：两张位图启动时生成一次，轮询期只赋引用，
    # 消除每次翻转的 Pillow 重建开销。
    icon_images = {
        True: make_icon_image(COLOR_ONLINE),
        False: make_icon_image(COLOR_OFFLINE),
    }

    def _stop_icon(reason: str) -> None:
        """统一退出三步（菜单退出与外部事件退出共用）：置停 → 唤醒等待 →
        ``icon.stop()``（Windows 下即发 NIM_DELETE 移除通知区图标）。"""
        logger.info("%s", reason)
        icon_holder["stopped"] = True
        ev = icon_holder.get("stop_event")
        if ev is not None:
            ev.set()
        ic = icon_holder.get("icon")
        if ic is not None:
            try:
                ic.stop()
            except Exception:  # noqa: BLE001（重复 stop 等：不影响退出流程）
                logger.warning("icon.stop() 异常（忽略）", exc_info=True)

    def _poll_loop() -> None:
        """状态轮询线程（托盘唯一轻量轮询，§5.2）。"""
        while not icon_holder.get("stopped"):
            # 探测前移（修复 2）：退出事件非阻塞探测置于循环体**最前**（工作段之前）。
            # 旧实现把探测放在状态抓取/图标更新（含 fetch_status，HTTP 超时 3s）之后，
            # 最坏响应延迟 ≈ 5s 等待 + 3s HTTP = 8s，与 tray-exit 默认 8s 超时贴边，
            # 超时即退回 taskkill（幽灵图标回归）；前移后最坏延迟 ≤1 个 POLL_INTERVAL，
            # 且 iss 侧已显式传 --timeout 15，余量充足。探测自身的 try/except 降级
            # 保留：任何异常记日志后降级为纯等待，轮询线程永不因探测异常而死。
            stop_event = icon_holder.get("stop_event")
            try:
                if exit_event_handle is not None:
                    import win32event

                    if (win32event.WaitForSingleObject(exit_event_handle, 0)
                            == win32event.WAIT_OBJECT_0):
                        _stop_icon("外部请求退出（卸载/升级）——走 icon.stop() "
                                   "发 NIM_DELETE，防通知区幽灵图标")
                        return
            except Exception:  # noqa: BLE001
                logger.exception("退出事件探测异常（降级为纯 stop_event.wait，"
                                 "轮询线程继续）")
            try:
                token = load_local_token()  # 每轮重读：服务重启会换令牌
                state, body = fetch_status(port, token)
                icon = icon_holder.get("icon")
                if icon is not None:
                    # 图标自愈（Task #2）：删除原 last_online 边沿缓存分支，
                    # 每轮**无条件**重赋缓存位图——若 pystray 的
                    # Shell_NotifyIcon 某轮静默失败（图标灰死但状态文字
                    # 正常），最多一个 POLL_INTERVAL 后即被下一轮重赋覆盖；
                    # 位图来自 icon_images 预缓存，重赋无重建开销。
                    # tooltip 内容随活跃任务数变化，每轮更新不变。
                    icon.icon = icon_images[state == ST_ONLINE]
                    icon.title = build_tooltip(state, body)
                notice = tracker.apply(state)
                if notice and icon is not None:
                    # 评审问题 3：凭证态离线改用凭证文案（措辞区分见 pick_notice）
                    notice = pick_notice(state, body)
                    try:
                        icon.notify(notice, NOTIFY_TITLE)
                    except Exception:
                        logger.warning("气泡提示发送失败（不影响轮询）", exc_info=True)
                logger.debug("状态轮询: state=%s ws=%s token_status=%s",
                             state, (body or {}).get("ws_connected"),
                             (body or {}).get("token_status", "ok"))
            except Exception:
                logger.exception("状态轮询异常（继续下轮）")
            # 可被退出打断的等待（评审问题 1 修复）：
            # 旧实现把 threading.Event 塞进 WaitForMultipleObjects 句柄列表，
            # pywin32 抛 TypeError 且该段在 try/except 之外，直接杀死轮询线程。
            # 现保持 stop_event.wait(POLL_INTERVAL) 原语义（可被菜单退出打断）；
            # 退出事件探测已前移至循环体最前（修复 2），响应延迟最坏 ≤1 个
            # POLL_INTERVAL（5s），远在 tray-exit --timeout 15 窗口内。
            if stop_event is not None and stop_event.wait(POLL_INTERVAL):
                return

    def _quit(icon, _item) -> None:
        _stop_icon("用户退出托盘（不影响服务运行）")

    menu = Menu(
        MenuItem("打开控制台", lambda icon, item: open_console(port, logger)),
        MenuItem("打开日志目录", lambda icon, item: open_logs_dir(logger)),
        Menu.SEPARATOR,
        MenuItem("退出", _quit),
    )
    icon = pystray.Icon(
        "SAUTray",
        icon_images[False],  # 启动初始为离线灰，首轮轮询后按状态重赋
        f"SAU Agent v{APP_VERSION} | 正在连接…",
        menu,
    )
    icon_holder["icon"] = icon
    icon_holder["stop_event"] = threading.Event()

    poll_thread = threading.Thread(target=_poll_loop, name="sau-tray-poll", daemon=True)
    poll_thread.start()
    try:
        icon.run()  # 阻塞（Windows 消息循环），退出菜单后返回
    except Exception:
        logger.exception("托盘运行异常退出（不影响服务）")
        return 1
    finally:
        icon_holder["stopped"] = True
        ev = icon_holder.get("stop_event")
        if ev is not None:
            ev.set()
        try:
            import win32api

            win32api.CloseHandle(mutex)
        except Exception:  # pragma: no cover
            pass
        if exit_event_handle is not None:
            try:
                import win32api

                win32api.CloseHandle(exit_event_handle)
            except Exception:  # pragma: no cover
                pass
        logger.info("托盘已退出")
    return 0
