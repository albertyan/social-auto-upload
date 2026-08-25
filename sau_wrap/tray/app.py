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


def build_tooltip(state: str, body: dict | None) -> str:
    """tooltip：版本 / 连接态 / 活跃任务数。"""
    if state == ST_UNREACHABLE:
        return f"SAU Agent v{APP_VERSION} | 服务不可达"
    if state == ST_AUTH:
        return f"SAU Agent v{APP_VERSION} | 令牌不匹配（服务可能刚重启）"
    if not body:
        return f"SAU Agent v{APP_VERSION}"
    connected = "在线" if body.get("ws_connected") else "未连接"
    if body.get("suspended"):
        connected = "已挂起"
    return (
        f"SAU Agent v{body.get('version', APP_VERSION)} | "
        f"连接: {connected} | 活跃任务: {body.get('active_tasks', 0)}"
    )


def classify_http(status_code: int, body: dict | None) -> str:
    """HTTP 结果分类（四态：在线 / 离线 / 401 / 不可达由调用方单独给出）。"""
    if status_code == 401:
        return ST_AUTH
    if status_code == 200 and body and body.get("ws_connected") and not body.get("suspended"):
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

    port = resolve_local_port()
    tracker = StateTracker()
    icon_holder: dict = {}

    def _poll_loop() -> None:
        """状态轮询线程（托盘唯一轻量轮询，§5.2）。"""
        while not icon_holder.get("stopped"):
            try:
                token = load_local_token()  # 每轮重读：服务重启会换令牌
                state, body = fetch_status(port, token)
                icon = icon_holder.get("icon")
                if icon is not None:
                    # 图标按状态缓存：仅在线/离线翻转时重赋，避免每轮重建位图；
                    # tooltip 内容随活跃任务数变化，每轮更新不变。
                    is_online = state == ST_ONLINE
                    if icon_holder.get("last_online") != is_online:
                        icon.icon = make_icon_image(pick_icon_color(state))
                        icon_holder["last_online"] = is_online
                    icon.title = build_tooltip(state, body)
                notice = tracker.apply(state)
                if notice and icon is not None:
                    try:
                        icon.notify(notice, NOTIFY_TITLE)
                    except Exception:
                        logger.warning("气泡提示发送失败（不影响轮询）", exc_info=True)
                logger.debug("状态轮询: state=%s ws=%s",
                             state, (body or {}).get("ws_connected"))
            except Exception:
                logger.exception("状态轮询异常（继续下轮）")
            # 可被退出打断的等待
            stopped = icon_holder.get("stop_event")
            if stopped is not None and stopped.wait(POLL_INTERVAL):
                return

    def _quit(icon, _item) -> None:
        logger.info("用户退出托盘（不影响服务运行）")
        icon_holder["stopped"] = True
        ev = icon_holder.get("stop_event")
        if ev is not None:
            ev.set()
        icon.stop()

    menu = Menu(
        MenuItem("打开控制台", lambda icon, item: open_console(port, logger)),
        MenuItem("打开日志目录", lambda icon, item: open_logs_dir(logger)),
        Menu.SEPARATOR,
        MenuItem("退出", _quit),
    )
    icon = pystray.Icon(
        "SAUTray",
        make_icon_image(COLOR_OFFLINE),
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
        logger.info("托盘已退出")
    return 0
