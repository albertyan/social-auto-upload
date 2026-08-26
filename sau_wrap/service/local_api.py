# -*- coding: utf-8 -*-
"""5409 本地 API（实施计划 S3；S6 扩展控制台托管与鉴权链路）。

设计文档：§3.5 端点清单、§3.7 契约表、§4.4 端口决策、§6.3/§6.4 控制台鉴权。

要点：
- 仅监听 ``127.0.0.1:5409``（端口可由 config.json ``local_api_port`` 覆盖，§4.4）；
- **绑定失败必须明确报错并记日志**（提示端口可能被上游遗留 ``sau_backend.py`` 或其他
  进程占用），禁止静默失败（§4.4）；服务主体（WS 主循环）不受影响继续运行；
- **双鉴权并存**（§6.3）：
  ① ``X-SAU-Local-Token`` 头——服务每次启动重新生成，写入
  ``%ProgramData%\\SAU\\local_token.bin``（users 可读，§3.6），供托盘 / CLI 调用；
  ② Cookie 会话——控制台浏览器**永不接触本地令牌**（§3.6），一律走
  「一次性票据 → 换 Cookie」链路；
- **S6 控制台鉴权链路**（§6.3 定案）：
  ``POST /ui-ticket``（需本地令牌）签发一次性票据（``secrets.token_urlsafe(32)``、
  内存表、60 秒过期、单次核销）→ ``GET /ui/t/<ticket>`` 核销并种会话 Cookie
  （HttpOnly、SameSite=Strict）→ 302 到 ``/ui/``；会话**单实例顶替**（新会话顶掉
  旧会话）、30 分钟无活动失效（服务重启会话失效可接受，§6.4）；
- **写操作 Nonce 双重防护**（§6.3）：Cookie 会话发起的写操作必须携带
  ``X-Console-Nonce``（先 ``GET /nonce`` 领取，一次性消费、短窗去重）；令牌鉴权
  （托盘/CLI）不经浏览器上下文，无 CSRF 面，豁免 Nonce；
- **静态托管**（§6.4）：``GET /ui/*`` 从控制台构建产物目录（默认
  ``sau_wrap/console/dist``，打包后为 ``{app}\\ui``，§6.7）提供；路径穿越防护；
  hash 路由无需服务端回退；dist 不存在返回友好提示页；
- 本步端点：``GET /status``、``GET/POST /config``、``POST /reload``、``POST /bind``、
  ``GET /machine-code``、``GET /nonce``、``POST /ui-ticket``、``GET /ui/t/<ticket>``、
  ``GET /ui/*``；S7 起升级族落地：``GET /upgrade``（只读快照）、
  ``POST /upgrade/apply``（用户确认触发编排）、``POST /upgrade/snooze``（稍后提醒）；
  S9 起登录会话族落地（§6.5）：``POST /login/{platform}``、
  ``GET /login/qrcode/{session_id}``、``GET /login/status/{session_id}``、
  ``POST /login/{session_id}/code``、``DELETE /login/{session_id}``（及 ``.../cancel``）、
  ``GET /accounts/status``、``DELETE /accounts``；``/accounts/recheck`` 仍占位 501；
- 写操作审计：绑定/配置写入记一行审计日志（时间、操作、来源恒 127.0.0.1、结果、鉴权方式）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import secrets
import sys
import time
from pathlib import Path

from aiohttp import web

from sau_wrap import paths
from sau_wrap.agent import config as agent_config

#: 默认本地 API 端口（§4.4 定案；config.json 可覆盖）
DEFAULT_PORT = 5409

#: 一次性票据有效期（秒，§6.3：60 秒）
UI_TICKET_TTL = 60.0

#: 会话无活动超时（秒，§6.3：30 分钟）
SESSION_TIMEOUT = 30 * 60

#: 会话 Cookie 名
SESSION_COOKIE = "sau_session"

#: Nonce 短窗去重保留时长（秒）：已签发未消费的 nonce 超过该时长作废
NONCE_WINDOW = 600.0

#: Cookie 会话写操作必须携带 Nonce 的路径（§6.3：/config、/upgrade/apply 等；
#: S7 起升级写端点并入；S9 起登录会话写族与账号删除并入（前缀匹配，
#: 见 _nonce_required）；令牌鉴权豁免，见中间件）
_NONCE_REQUIRED = frozenset({"/config", "/bind", "/reload",
                             "/upgrade/apply", "/upgrade/snooze"})


def _nonce_required(method: str, path: str) -> bool:
    """Cookie 会话写操作 Nonce 约束判定（§6.3；S9 扩展登录/账号写族）。

    GET 类（含 /login/qrcode 与 /login/status 轮询）不受约束；
    ``/login/*`` 的 POST/DELETE 与 ``DELETE /accounts`` 属写操作，纳入防护。
    """
    if method not in ("POST", "DELETE"):
        return False
    if path in _NONCE_REQUIRED:
        return True
    if path.startswith("/login/"):
        return True
    return path == "/accounts"

#: 401 引导页（§6.3：未认证访问控制台 → 说明从托盘「打开控制台」进入）
_UNAUTH_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>SAU 控制台 - 会话未认证</title>
<style>body{font-family:system-ui,sans-serif;display:flex;justify-content:center;
padding-top:12vh;background:#f5f6f7;color:#333}
.card{background:#fff;border-radius:8px;padding:32px 40px;max-width:480px;
box-shadow:0 2px 8px rgba(0,0,0,.08)}h1{font-size:18px}p{line-height:1.7}</style>
</head><body><div class="card">
<h1>会话已失效或未认证</h1>
<p>请从系统托盘右键菜单「打开控制台」重新进入（托盘会自动换取一次性会话）。</p>
<p>若托盘未运行，请先启动 SAU Agent 托盘。</p>
</div></body></html>
"""

#: dist 不存在时的友好提示页（构建产物由 ``cd sau_wrap/console && npm run build``
#: 产出；安装包内为 {app}\\ui，§6.7，属 S8 打包步骤）
_NO_DIST_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>SAU 控制台 - 构建产物缺失</title>
<style>body{font-family:system-ui,sans-serif;display:flex;justify-content:center;
padding-top:12vh;background:#f5f6f7;color:#333}
.card{background:#fff;border-radius:8px;padding:32px 40px;max-width:520px;
box-shadow:0 2px 8px rgba(0,0,0,.08)}h1{font-size:18px}p,code{line-height:1.7}</style>
</head><body><div class="card">
<h1>控制台构建产物未找到</h1>
<p>开发环境请先执行：</p>
<p><code>cd sau_wrap/console &amp;&amp; npm install &amp;&amp; npm run build</code></p>
<p>构建产物输出至 <code>sau_wrap/console/dist</code>，刷新本页即可访问控制台。</p>
</div></body></html>
"""


def resolve_ui_dist_dir() -> Path:
    """控制台静态资源目录解析（§6.4/§6.7）。

    优先级：``SAU_UI_DIST_DIR`` 环境变量（测试/自定义）→ 打包后 ``{app}\\ui``
    （Nuitka standalone：``paths.is_frozen()``——Nuitka 不设 ``sys.frozen``，
    §7.1 --include-data-dir=console/dist=ui）→ 开发默认 ``sau_wrap/console/dist``。
    """
    import os

    env = os.environ.get("SAU_UI_DIST_DIR")
    if env:
        return Path(env)
    if paths.is_frozen():  # Nuitka/PyInstaller 打包后（§6.7）
        return Path(sys.executable).parent / "ui"
    return Path(__file__).resolve().parents[1] / "console" / "dist"


class LocalApiBindError(RuntimeError):
    """端口绑定失败（必须明确报错，禁止静默失败，§4.4）。"""


class LocalApiServer:
    """5409 本地 API 服务（与服务进程同进程，同一 asyncio 事件循环）。"""

    def __init__(
        self,
        logger: logging.Logger,
        ws_client,
        port: int = DEFAULT_PORT,
        ui_dist_dir: Path | None = None,
        ticket_ttl: float = UI_TICKET_TTL,
        session_timeout: float = SESSION_TIMEOUT,
        updater=None,
        login_manager=None,
    ) -> None:
        self._logger = logger
        self._client = ws_client
        self.port = port
        self.token: str = ""
        self._runner: web.AppRunner | None = None
        self.ui_dist_dir = ui_dist_dir or resolve_ui_dist_dir()
        self.ticket_ttl = ticket_ttl
        self.session_timeout = session_timeout
        #: S7：升级状态机（host 接线；未挂载时 GET /upgrade 读持久化快照，
        #: apply/snooze 返回 503）
        self._updater = updater
        #: S7：升级编排器（host 接线；可注入执行器，见 upgrade.orchestrator）
        self._orchestrator = None
        # ---- S9 登录会话管理器（§6.5；可注入执行器便于测试）----
        from sau_wrap.service.login_sessions import LoginSessionManager  # noqa: PLC0415

        self._login_manager = login_manager or LoginSessionManager(logger)
        # 成功后置动作由服务端统一接线（§6.5：自动 account_sync 上行），
        # 无论管理器是默认创建还是测试注入。
        self._login_manager.set_on_success(self._notify_account_sync)
        # ---- S6 会话状态（内存表，服务重启失效可接受，§6.4）----
        self._tickets: dict[str, float] = {}   # 一次性票据 → 过期时间戳
        self._sessions: dict[str, float] = {}  # 会话 id → 最近活动时间戳
        self._active_sid: str | None = None    # 单实例顶替：当前唯一活跃会话
        self._nonces: dict[str, float] = {}    # 已签发未消费 nonce → 签发时间

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """生成令牌 → 建路由 → 绑定端口。绑定失败抛 ``LocalApiBindError``。"""
        self._regenerate_token()
        app = web.Application(middlewares=[self._auth_middleware])
        self._register_routes(app)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        try:
            await site.start()
        except OSError as exc:
            msg = (
                f"本地 API 绑定 127.0.0.1:{self.port} 失败: {exc}。"
                f"端口可能被上游遗留 sau_backend.py 或其他进程占用"
                f"（§4.4：可在 config.json 的 local_api_port 覆盖）。"
            )
            self._logger.error(msg)
            await self._runner.cleanup()
            self._runner = None
            raise LocalApiBindError(msg) from exc
        self._logger.info("本地 API 已启动: http://127.0.0.1:%d（令牌已写入 %s）",
                          self.port, paths.LOCAL_TOKEN_FILE)
        if not (self.ui_dist_dir / "index.html").is_file():
            self._logger.warning(
                "控制台构建产物缺失或不完整: %s（/ui/ 将返回友好提示页；"
                "开发环境请先 cd sau_wrap/console && npm run build）",
                self.ui_dist_dir,
            )

    async def stop(self) -> None:
        # S9 收尾：停机先取消全部活跃登录会话（close_all），收敛执行器浏览器
        # 句柄，消除服务停止瞬间的孤儿进程窗口（优雅停机；幂等，重复调用无副作用）。
        await self._login_manager.close_all()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._logger.info("本地 API 已停止")

    def _regenerate_token(self) -> None:
        """服务每次启动重新生成令牌（§3.6），写入 local_token.bin（users 可读）。"""
        self.token = secrets.token_urlsafe(32)
        paths.ensure_dir(paths.DATA_ROOT)
        paths.LOCAL_TOKEN_FILE.write_bytes(self.token.encode("utf-8"))

    def attach_orchestrator(self, orchestrator) -> None:
        """挂载升级编排器（S7，host 接线；可注入执行器便于开发验证）。"""
        self._orchestrator = orchestrator

    # ------------------------------------------------------------ 鉴权中间件

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        """双鉴权中间件（aiohttp 3.14 需 @web.middleware 装饰）。

        - ``/ui/*``（静态资源与票据核销入口）公开（5409 仅绑定 127.0.0.1，§6.3）；
        - 业务端点：``X-SAU-Local-Token`` 或有效会话 Cookie 任一通过即可；
        - Cookie 会话的写操作追加 Nonce 双重防护（§6.3）。
        """
        path = request.path
        if path == "/ui" or path.startswith("/ui/"):
            return await handler(request)  # 静态托管 + /ui/t/<ticket> 核销

        supplied = request.headers.get("X-SAU-Local-Token", "")
        if supplied and secrets.compare_digest(supplied, self.token):
            request["auth_via"] = "token"
        elif self._session_valid(request):
            request["auth_via"] = "cookie"
        else:
            return self._unauthorized_response(request)

        # 写操作 Nonce 双重防护（§6.3）：仅约束 Cookie 会话（浏览器上下文存在
        # CSRF 面）；令牌鉴权（托盘/CLI）不经浏览器，豁免。
        if (request["auth_via"] == "cookie"
                and _nonce_required(request.method, path)):
            err = self._consume_nonce(request)
            if err is not None:
                return err
        return await handler(request)

    def _unauthorized_response(self, request: web.Request, error: str = "unauthorized",
                               message: str = "缺少或错误的凭证") -> web.Response:
        """401：浏览器直接访问返回引导页，API 调用返回 JSON（§6.3 统一拦截）。
        票据核销失败等浏览器导航场景复用本逻辑（按 Accept 判断，不裸 JSON）。"""
        accept = request.headers.get("Accept", "")
        if "text/html" in accept and "application/json" not in accept:
            return web.Response(status=401, content_type="text/html",
                                charset="utf-8", text=_UNAUTH_HTML)
        return web.json_response(
            {"error": error,
             "message": message,
             "guide": "会话已失效，请从托盘「打开控制台」重新进入"},
            status=401,
        )

    # ------------------------------------------------------------ 会话与票据（§6.3）

    def issue_ticket(self) -> str:
        """签发一次性票据：``token_urlsafe(32)``，内存表，60 秒过期、单次核销。"""
        now = time.time()
        self._tickets = {t: e for t, e in self._tickets.items() if e > now}
        ticket = secrets.token_urlsafe(32)
        self._tickets[ticket] = now + self.ticket_ttl
        return ticket

    def _redeem_ticket(self, ticket: str) -> str | None:
        """核销票据（一次性消费即删）；成功返回错误码字符串，失败语义相反——
        返回值：``None``=核销成功；否则为错误描述。"""
        expire_at = self._tickets.pop(ticket, None)
        if expire_at is None:
            return "ticket_invalid"
        if expire_at < time.time():
            return "ticket_expired"
        return None

    def _new_session(self) -> str:
        """创建新会话（**单实例顶替**：新会话顶掉旧会话，§6.3）。"""
        sid = secrets.token_urlsafe(32)
        if self._active_sid is not None:
            self._sessions.pop(self._active_sid, None)  # 顶掉旧会话
            self._logger.info("控制台新会话顶替旧会话（单实例策略）")
        now = time.time()
        self._sessions[sid] = now
        self._active_sid = sid
        return sid

    def _session_valid(self, request: web.Request) -> bool:
        """校验会话 Cookie：存在、未超 30 分钟无活动（滑动续期）。"""
        sid = request.cookies.get(SESSION_COOKIE, "")
        last_active = self._sessions.get(sid)
        if last_active is None:
            return False
        now = time.time()
        if now - last_active > self.session_timeout:
            self._sessions.pop(sid, None)
            if self._active_sid == sid:
                self._active_sid = None
            return False
        self._sessions[sid] = now  # 滑动续期
        return True

    # ------------------------------------------------------------ Nonce（§6.3）

    def issue_nonce(self) -> str:
        """签发写操作 Nonce（短窗去重：超过 NONCE_WINDOW 未消费即作废）。"""
        now = time.time()
        self._nonces = {n: t for n, t in self._nonces.items()
                        if now - t <= NONCE_WINDOW}
        nonce = secrets.token_urlsafe(16)
        self._nonces[nonce] = now
        return nonce

    def _consume_nonce(self, request: web.Request) -> web.Response | None:
        """消费 ``X-Console-Nonce``：缺失→403；未知/已消费/过期→409；成功返回 None。"""
        supplied = request.headers.get("X-Console-Nonce", "").strip()
        if not supplied:
            return web.json_response(
                {"error": "nonce_required",
                 "message": "控制台写操作必须携带 X-Console-Nonce（先 GET /nonce 领取）"},
                status=403,
            )
        issued_at = self._nonces.pop(supplied, None)
        if issued_at is None or time.time() - issued_at > NONCE_WINDOW:
            return web.json_response(
                {"error": "nonce_invalid_or_reused",
                 "message": "Nonce 无效或已被使用（一次性消费，请重新 GET /nonce）"},
                status=409,
            )
        return None

    # ------------------------------------------------------------ 路由

    def _register_routes(self, app: web.Application) -> None:
        app.router.add_get("/status", self._get_status)
        app.router.add_get("/config", self._get_config)
        app.router.add_post("/config", self._post_config)
        app.router.add_post("/reload", self._post_reload)
        app.router.add_post("/bind", self._post_bind)
        # S6 新增：机器码只读端点 / nonce 签发 / 票据签发与核销 / 静态托管
        app.router.add_get("/machine-code", self._get_machine_code)
        app.router.add_get("/nonce", self._get_nonce)
        app.router.add_post("/ui-ticket", self._post_ui_ticket)
        # 注意：/ui 与 /ui/t/{ticket} 必须先于 /ui/{tail:.*} 注册（动态路由按注册顺序匹配）
        app.router.add_get("/ui", self._get_ui_root)
        app.router.add_get("/ui/t/{ticket}", self._get_ui_exchange)
        app.router.add_get("/ui/{tail:.*}", self._get_ui_static)
        # S7：升级族（§7.4：快照只读 / apply 确认编排 / snooze 稍后提醒）
        app.router.add_get("/upgrade", self._get_upgrade)
        app.router.add_post("/upgrade/apply", self._post_upgrade_apply)
        app.router.add_post("/upgrade/snooze", self._post_upgrade_snooze)
        # S9：登录会话族（§6.5：创建/二维码/状态/验证码注入/取消）
        # 注意：/login/qrcode 与 /login/status 必须先于 /login/{session_id} 族注册；
        # /login/{platform}（POST）与 /login/{session_id} 族同方法不冲突靠路径段区分——
        # aiohttp 动态路由均为单段匹配，此处平台/会话 id 同为单段，故创建端点用独立前缀语义：
        # POST /login/{platform}（创建）与 POST /login/{session_id}/code（注入）段数不同不冲突。
        app.router.add_post("/login/{platform}", self._post_login_start)
        app.router.add_get("/login/qrcode/{session_id}", self._get_login_qrcode)
        app.router.add_get("/login/status/{session_id}", self._get_login_status)
        app.router.add_post("/login/{session_id}/code", self._post_login_code)
        app.router.add_post("/login/{session_id}/cancel", self._post_login_cancel)
        app.router.add_delete("/login/{session_id}", self._post_login_cancel)
        # S9：账号族（§3.5：状态查询 / 删除；真实浏览器复核 /accounts/recheck 仍占位）
        app.router.add_get("/accounts/status", self._get_accounts_status)
        app.router.add_delete("/accounts", self._delete_account)
        # 占位端点（后续步骤实现）
        for method, path, note in _PLACEHOLDER_ROUTES:
            app.router.add_route(method, path, self._make_placeholder(note))

    @staticmethod
    def _make_placeholder(note: str):
        async def handler(request: web.Request) -> web.Response:  # noqa: ANN202
            return web.json_response(
                {"error": "not_implemented", "message": f"本步占位，尚未实现：{note}"},
                status=501,
            )
        return handler

    # ------------------------------------------------------------ S6 控制台端点

    async def _post_ui_ticket(self, request: web.Request) -> web.Response:
        """``POST /ui-ticket``：签发一次性票据（仅本地令牌可签发；控制台浏览器
        永不接触本地令牌，§3.6）。"""
        if request.get("auth_via") != "token":
            return web.json_response(
                {"error": "token_required",
                 "message": "票据签发仅限 X-SAU-Local-Token 调用方（托盘/CLI）"},
                status=403,
            )
        ticket = self.issue_ticket()
        self._logger.info("[AUDIT] op=ui_ticket source=127.0.0.1 result=success "
                          "detail=ttl=%ss via=token", int(self.ticket_ttl))
        return web.json_response({"ticket": ticket,
                                  "expires_seconds": int(self.ticket_ttl)})

    async def _get_ui_root(self, request: web.Request) -> web.Response:
        """``GET /ui``（无尾斜杠）→ 302 ``/ui/``（体验项：避免 404）。"""
        return web.HTTPFound("/ui/")

    async def _get_ui_exchange(self, request: web.Request) -> web.Response:
        """``GET /ui/t/<ticket>``：核销一次性票据 → 种会话 Cookie → 302 ``/ui/``。

        核销失败（无效/过期）对浏览器导航返回引导 HTML（按 Accept 判断，
        复用 ``_unauthorized_response`` 逻辑），不再裸 JSON。
        """
        ticket = request.match_info.get("ticket", "")
        err = self._redeem_ticket(ticket)
        if err is not None:
            return self._unauthorized_response(
                request, error=err,
                message="票据无效或已过期，请从托盘「打开控制台」重新进入")
        sid = self._new_session()
        resp = web.HTTPFound("/ui/")
        resp.set_cookie(SESSION_COOKIE, sid, path="/",
                        max_age=int(self.session_timeout),
                        httponly=True, samesite="Strict")
        return resp

    async def _get_nonce(self, request: web.Request) -> web.Response:
        """``GET /nonce``：签发写操作 Nonce（§6.3 CSRF 双重防护）。"""
        return web.json_response({"nonce": self.issue_nonce()})

    async def _get_machine_code(self, request: web.Request) -> web.Response:
        """``GET /machine-code``：机器码只读展示（绑定页，§5.7；复用注册同一算法）。"""
        from sau_wrap.agent.machine import get_machine_code

        try:
            code = get_machine_code()
        except RuntimeError as exc:
            return web.json_response(
                {"error": "machine_code_unavailable", "message": str(exc)}, status=503)
        return web.json_response({"machine_code": code})

    async def _get_ui_static(self, request: web.Request) -> web.Response:
        """``GET /ui/*``：静态托管（§6.4）。

        - dist 不存在或产物不完整（无 index.html）→ 友好提示页（含构建指引）；
        - 路径穿越防护：拒绝 ``..``/反斜杠，且解析后必须仍在 dist 目录内；
        - hash 路由无需服务端回退：未知路径一律 404；
        - ``index.html`` no-cache，其余资源长缓存（§6.4）。
        """
        if not (self.ui_dist_dir / "index.html").is_file():
            return web.Response(status=503, content_type="text/html",
                                charset="utf-8", text=_NO_DIST_HTML)
        tail = request.match_info.get("tail", "") or ""
        if tail in ("", "/"):
            tail = "index.html"
        # 路径穿越防护（§6.4）
        if ".." in tail or "\\" in tail or tail.startswith("/"):
            return web.Response(status=400, text="bad path")
        dist_root = self.ui_dist_dir.resolve()
        target = (dist_root / tail).resolve()
        try:
            target.relative_to(dist_root)
        except ValueError:
            return web.Response(status=400, text="bad path")
        if not target.is_file():
            return web.Response(status=404, text="not found")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        if target.name == "index.html":
            headers = {"Cache-Control": "no-cache"}
        else:
            headers = {"Cache-Control": "max-age=86400"}
        return web.Response(body=data, content_type=ctype, headers=headers)

    # ------------------------------------------------------------ S7 升级端点（§7.4）

    async def _get_upgrade(self, request: web.Request) -> web.Response:
        """``GET /upgrade``：只读快照（阶段/目标版本/下载进度/校验结果/错误）。

        未挂载 updater 时退化为读持久化状态文件（服务重启后仍可展示历史状态）。
        """
        if self._updater is not None:
            return web.json_response(self._updater.snapshot())
        from sau_wrap.upgrade.updater import read_state_file, _empty_state, PHASES  # noqa: PLC0415
        st = read_state_file() or _empty_state()
        return web.json_response({
            "phase": st.get("phase"), "version": st.get("version"),
            "download_url": st.get("download_url"),
            "installer_path": st.get("installer_path"),
            "progress": {"downloaded_bytes": st.get("downloaded_bytes") or 0,
                          "total_bytes": st.get("total_bytes") or 0,
                          "percent": None},
            "verified": bool(st.get("verified")), "error": st.get("error"),
            "last_rejected": st.get("last_rejected"),
            "updated_at": st.get("updated_at"),
            "note": "升级模块未挂载，仅持久化快照" if st.get("phase") in PHASES
                    else "尚未收到升级通知",
        })

    async def _post_upgrade_apply(self, request: web.Request) -> web.Response:
        """``POST /upgrade/apply``：用户确认触发编排（控制台是唯一确认入口，§7.4）。

        编排为同步阻塞流程（真实环境含停服/安装），以 ``to_thread`` 后台执行；
        本步开发验证注入假执行器（真机链路留待 S8 打包后）。

        单飞语义（review 修正）：端点检查通过后**同步**置 ``applying`` 再返回，
        重复 apply 因 phase 已非 ready/snoozed 直接 409；编排器内部另有线程锁兜底。
        """
        if self._updater is None:
            return web.json_response(
                {"error": "updater_not_attached", "message": "升级模块未挂载"},
                status=503)
        if self._orchestrator is None:
            return web.json_response(
                {"error": "orchestrator_not_attached",
                 "message": "升级编排器未挂载（服务重启后将自动重建）"},
                status=503)
        updater = self._updater
        phase = updater.state.get("phase")
        if phase not in ("ready", "snoozed"):
            return web.json_response(
                {"error": "upgrade_not_ready",
                 "message": f"当前状态 {phase or '无'} 不可执行升级（需 ready/snoozed）",
                 "phase": phase},
                status=409)
        # 同步置 applying 再后台执行：编排第一步即停服，本进程可能在步骤中退出，
        # 状态已持久化 → 重启后由启动自检三分支收敛（§15.2）。
        updater.set_phase("applying", error=None)
        orchestrator = self._orchestrator
        asyncio.get_running_loop().create_task(
            asyncio.to_thread(self._run_orchestration, orchestrator, request))
        return web.json_response({"ok": True, "phase": "applying",
                                   "message": "升级已确认，编排已启动"})

    def _run_orchestration(self, orchestrator, request: web.Request) -> None:
        """编排后台线程包装：异常兜底落 failed + 审计。"""
        try:
            result = orchestrator.apply()
            self._audit("upgrade_apply",
                        "success" if result.get("ok") else "fail",
                        f"phase={result.get('phase')} "
                        f"failed_step={result.get('failed_step', '-')}"
                        f" rollback_failed_at={result.get('rollback_failed_at', '-')}",
                        request)
        except Exception as exc:
            self._updater.set_phase("failed", error=f"编排异常：{exc}")
            self._logger.exception("升级编排异常")
            self._audit("upgrade_apply", "fail", f"exception={exc}", request)

    async def _post_upgrade_snooze(self, request: web.Request) -> web.Response:
        """``POST /upgrade/snooze``：稍后提醒（``ready → snoozed``，§7.4）。"""
        if self._updater is None:
            return web.json_response(
                {"error": "updater_not_attached", "message": "升级模块未挂载"},
                status=503)
        updater = self._updater
        if updater.state.get("phase") == "snoozed":
            return web.json_response({"ok": True, "phase": "snoozed",
                                       "message": "已处于稍后提醒状态"})
        if updater.state.get("phase") != "ready":
            return web.json_response(
                {"error": "upgrade_not_ready",
                 "message": "仅 ready 状态可稍后提醒",
                 "phase": updater.state.get("phase")},
                status=409)
        updater.set_phase("snoozed")
        self._audit("upgrade_snooze", "success",
                    f"version={updater.state.get('version')}", request)
        return web.json_response({"ok": True, "phase": "snoozed"})

    # ------------------------------------------------------------ S9 登录会话端点（§6.5）

    async def _notify_account_sync(self, session) -> None:
        """登录成功后置动作（§6.5）：触发 account_sync 上行更新账号快照。

        cookie 已由执行器落主目录，``scan_accounts`` 自动可见；
        WS 未连接时 ``send_account_sync`` 返回 False（下次心跳带快照兜底）。
        """
        sender = getattr(self._client, "send_account_sync", None)
        if sender is None:
            return
        await sender()

    async def _post_login_start(self, request: web.Request) -> web.Response:
        """``POST /login/{platform}``：创建登录会话（每平台单会话，§6.5）。

        请求体可选 ``{"account_name": "..."}``（缺省 default）。
        错误：平台不支持 → 400；内核未装 → 503（引导 browser install）；
        已有活跃会话 → 409（携带既有 session_id）。
        """
        from sau_wrap.service import login_sessions as ls  # noqa: PLC0415

        platform = request.match_info.get("platform", "")
        account_name = "default"
        try:
            body = await request.json()
            account_name = str(body.get("account_name") or "default")
        except (ValueError, json.JSONDecodeError, AttributeError):
            pass
        try:
            session = await self._login_manager.create(platform, account_name)
        except ls.LoginPlatformUnsupportedError as exc:
            return web.json_response(
                {"error": "platform_unsupported", "message": str(exc)}, status=400)
        except ls.LoginBrowserMissingError as exc:
            return web.json_response(
                {"error": "browser_missing", "message": str(exc),
                 "guide": "sau.exe browser install"},
                status=503)
        except ls.LoginSessionConflictError as exc:
            return web.json_response(
                {"error": "session_conflict",
                 "message": "该平台已有进行中的登录会话",
                 "session_id": exc.existing_session_id},
                status=409)
        self._audit("login_start", "success",
                    f"platform={platform} account={account_name} "
                    f"session={session.session_id}", request)
        return web.json_response(session.to_status_dict())

    async def _get_login_qrcode(self, request: web.Request) -> web.Response:
        """``GET /login/qrcode/{session_id}``：二维码图片（PNG；未就绪 404）。
        前端每 2s 轮询；上游刷新二维码后回调覆盖，轮询即得新图。"""
        session = self._login_manager.get(request.match_info["session_id"])
        if session is None:
            return web.json_response({"error": "session_not_found"}, status=404)
        if not session.qrcode_bytes:
            return web.json_response(
                {"error": "qrcode_not_ready",
                 "message": "二维码尚未就绪（浏览器启动/页面加载中）",
                 "status": session.status},
                status=404)
        return web.Response(body=session.qrcode_bytes, content_type="image/png",
                            headers={"Cache-Control": "no-store",
                                     "X-Qrcode-Updated-At": str(int(session.qrcode_updated_at))})

    async def _get_login_status(self, request: web.Request) -> web.Response:
        """``GET /login/status/{session_id}``：状态机轮询（前端每 2s）。"""
        session = self._login_manager.get(request.match_info["session_id"])
        if session is None:
            return web.json_response({"error": "session_not_found"}, status=404)
        return web.json_response(session.to_status_dict())

    async def _post_login_code(self, request: web.Request) -> web.Response:
        """``POST /login/{session_id}/code``：短信验证码注入（§6.5 need_input）。"""
        session = self._login_manager.get(request.match_info["session_id"])
        if session is None:
            return web.json_response({"error": "session_not_found"}, status=404)
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            return web.json_response({"error": "invalid_json"}, status=400)
        code = str(body.get("code") or "").strip()
        if not code:
            return web.json_response(
                {"error": "code_required", "message": "验证码不能为空"}, status=400)
        if not session.inject_code(code):
            return web.json_response(
                {"error": "not_awaiting_code",
                 "message": f"当前状态 {session.status} 不在等待验证码",
                 "status": session.status},
                status=409)
        self._audit("login_code", "success",
                    f"session={session.session_id} platform={session.platform}",
                    request)
        return web.json_response({"ok": True, "status": session.status})

    async def _post_login_cancel(self, request: web.Request) -> web.Response:
        """``DELETE /login/{session_id}``（及 ``POST .../cancel``）：取消会话。"""
        session = await self._login_manager.cancel(request.match_info["session_id"])
        if session is None:
            return web.json_response({"error": "session_not_found"}, status=404)
        self._audit("login_cancel", "success",
                    f"session={session.session_id} platform={session.platform}",
                    request)
        return web.json_response({"ok": True, "status": session.status})

    # ------------------------------------------------------------ S9 账号端点（§3.5）

    async def _get_accounts_status(self, request: web.Request) -> web.Response:
        """``GET /accounts/status``：账号列表（双目录兼容扫描，§3.6）。

        基础判定：文件存在且合法 JSON 即 is_valid；真实浏览器复核标注后续
        （``/accounts/recheck`` 占位）。
        """
        from sau_wrap.agent import accounts as accounts_mod  # noqa: PLC0415

        return web.json_response({"accounts": accounts_mod.scan_accounts(),
                                  "note": "is_valid 为基础判定（文件存在且合法 JSON）；"
                                          "真实浏览器复核待 /accounts/recheck 实现"})

    async def _delete_account(self, request: web.Request) -> web.Response:
        """``DELETE /accounts``：删除主目录账号 cookie 文件（写操作：Nonce + 审计）。

        仅删主目录 ``%ProgramData%\\SAU\\cookies``；上游兼容目录只读回退，
        同名仅存在于兼容目录时 409 说明（不删上游仓库文件）。
        """
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            return web.json_response({"error": "invalid_json"}, status=400)
        platform = str(body.get("platform") or "").strip()
        account = str(body.get("account") or "").strip()
        if not platform or not account:
            return web.json_response(
                {"error": "platform_and_account_required"}, status=400)
        target = paths.COOKIES_DIR / f"{platform}_{account}.json"
        if target.is_file():
            try:
                target.unlink()
            except OSError as exc:
                self._audit("account_delete", "fail",
                            f"{platform}_{account} err={exc}", request)
                return web.json_response(
                    {"error": "delete_failed", "message": str(exc)}, status=500)
            self._audit("account_delete", "success",
                        f"{platform}_{account}", request)
            return web.json_response({"ok": True,
                                       "message": f"已删除 {platform}_{account}"})
        from sau_wrap.agent import accounts as accounts_mod  # noqa: PLC0415
        if accounts_mod.find_account_file(platform, account) is not None:
            return web.json_response(
                {"error": "fallback_readonly",
                 "message": "该账号仅存在于上游兼容目录（只读回退，包装层不删）"},
                status=409)
        return web.json_response({"error": "account_not_found"}, status=404)

    # ------------------------------------------------------------ 既有端点实现

    async def _get_status(self, request: web.Request) -> web.Response:
        """``GET /status``：服务状态（托盘轮询项，字段结构按 §3.7 契约固定）。"""
        client = self._client
        cfg = agent_config.load_config()
        snap = client.status_snapshot()
        if snap["token_expired"]:
            token_status = "expired"
        elif snap["suspended"]:
            token_status = "suspended"
        elif cfg is None:
            token_status = "unbound"
        else:
            token_status = "ok"
        body = {
            "ws_connected": snap["ws_connected"],
            "suspended": snap["suspended"],
            "agent_id": cfg.agent_id if cfg else None,
            "version": snap["version"],
            "active_tasks": snap["active_tasks"],
            "accounts": snap["accounts"],  # 账号快照骨架（S5/S6：Scanner 接入后填充）
            "clock_offset_seconds": snap["clock_offset_seconds"],
            "scheduling_paused": snap["scheduling_paused"],
            "token_expire_at": snap["token_expire_at"],
            "token_status": token_status,
            "last_close_reason": snap["last_close_reason"],
        }
        return web.json_response(body)

    async def _get_config(self, request: web.Request) -> web.Response:
        """``GET /config``：读绑定配置（不含凭证）。"""
        cfg = agent_config.load_config()
        if cfg is None:
            return web.json_response({"bound": False})
        return web.json_response({
            "bound": True,
            "server_url": cfg.server_url,
            "agent_id": cfg.agent_id,
            "heartbeat_interval": cfg.heartbeat_interval,
            "local_api_port": cfg.local_api_port,
        })

    async def _post_config(self, request: web.Request) -> web.Response:
        """``POST /config``：写 server_url（及可选运行参数）→ 触发热重载。"""
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            return web.json_response({"error": "invalid_json"}, status=400)
        server_url = str(body.get("server_url") or "").strip().rstrip("/")
        if not server_url.lower().startswith(("ws://", "wss://")):
            return web.json_response(
                {"error": "invalid_server_url", "message": "server_url 必须以 ws:// 或 wss:// 开头"},
                status=400,
            )
        cfg = agent_config.load_config()
        if cfg is None:
            self._audit("config", "fail", "未绑定（请先 POST /bind 或 sau bind）", request)
            return web.json_response(
                {"error": "unbound", "message": "尚未绑定，请先 POST /bind 或 sau bind"},
                status=409,
            )
        cfg.server_url = server_url
        if body.get("heartbeat_interval") is not None:
            try:
                cfg.heartbeat_interval = max(1, int(body["heartbeat_interval"]))
            except (TypeError, ValueError):
                return web.json_response({"error": "invalid_heartbeat_interval"}, status=400)
        port_note = ""
        if body.get("local_api_port") is not None:
            try:
                new_port = int(body["local_api_port"])
            except (TypeError, ValueError):
                return web.json_response({"error": "invalid_local_api_port"}, status=400)
            if new_port != cfg.local_api_port:
                cfg.local_api_port = new_port
                port_note = "（local_api_port 变更需服务重启生效）"
        agent_config.save_config(cfg)
        reload_msg = self._client.trigger_reload()
        self._audit("config", "success", f"server_url={server_url}", request)
        return web.json_response({
            "ok": True, "reload": reload_msg + port_note,
            "server_url": cfg.server_url,
        })

    async def _post_reload(self, request: web.Request) -> web.Response:
        """``POST /reload``：热重载——唤醒挂起态/断开当前连接以新配置重连。"""
        reload_msg = self._client.trigger_reload()
        self._audit("reload", "success", "-", request)
        return web.json_response({"ok": True, "reload": reload_msg})

    async def _post_bind(self, request: web.Request) -> web.Response:
        """``POST /bind``：复用 ``sau bind`` 同一逻辑（config.json + DPAPI 凭证）→ 热重载。"""
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            return web.json_response({"error": "invalid_json"}, status=400)
        server_url = str(body.get("server_url") or "")
        token = str(body.get("token") or "")
        agent_id = body.get("agent_id") or None
        try:
            cfg = agent_config.bind(server_url, token, agent_id)
        except (ValueError, RuntimeError) as exc:
            self._audit("bind", "fail", str(exc), request)
            return web.json_response({"error": "bind_failed", "message": str(exc)}, status=400)
        reload_msg = self._client.trigger_reload()
        self._audit("bind", "success",
                    f"server_url={cfg.server_url} agent_id={cfg.agent_id}", request)
        return web.json_response({
            "ok": True, "server_url": cfg.server_url, "agent_id": cfg.agent_id,
            "reload": reload_msg,
        })

    # ------------------------------------------------------------ 审计

    def _audit(self, operation: str, result: str, detail: str,
               request: web.Request | None = None) -> None:
        """写操作审计日志（§任务要求：时间由日志格式承载；来源恒 127.0.0.1；
        S6 起附鉴权方式 via=token|cookie 便于区分托盘/控制台来源）。"""
        via = (request.get("auth_via", "token") if request is not None else "token")
        self._logger.info(
            "[AUDIT] op=%s source=127.0.0.1 result=%s detail=%s via=%s",
            operation, result, detail, via,
        )


#: 占位端点（本步返回 501 + 说明，后续步骤实现；
#: /ui/* 与票据链路已由 S6 实现、升级族已由 S7 实现、登录会话族与账号
#: 查询/删除已由 S9 实现，从此清单移除；仅留真实浏览器复核）
_PLACEHOLDER_ROUTES = (
    ("POST", "/accounts/recheck", "账号状态真实浏览器复核（后续步骤）"),
)
