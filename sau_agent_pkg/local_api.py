"""
sau_agent_pkg.local_api
~~~~~~~~~~~~~~~~~~~~~~~
本地控制 HTTP API 服务器（aiohttp.web）。

监听 127.0.0.1:5410，所有请求需校验 X-SAU-Local-Token header。

接口：
- GET  /status            服务状态
- POST /login             返回支持的平台列表
- POST /accounts/recheck  触发全量 cookie 检查
- GET  /accounts/status   查询账号检查结果（task_id 轮询 / cached / scanned_only）
- DELETE /accounts        删除指定账号（query: platform=<platform>&account=<account>）
- POST /config            更新 server_url / 绑定 token
- GET  /config            读取非敏感配置
- POST /reload            配置热重载（重连 WS）
- GET  /upgrade           更新状态（upgrade_state.json 内容）
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from aiohttp import web

from sau_agent_pkg import accounts
from sau_agent_pkg.config import (
    get_agent_id,
    load_config,
    load_local_token,
    load_token,
    save_config,
    save_token,
)
from sau_agent_pkg.core import SauAgentCore
from sau_agent_pkg.upstream_adapter import PLATFORMS
from sau_agent_pkg.version import APP_VERSION

logger = logging.getLogger(__name__)

_LISTEN_HOST = "127.0.0.1"
_LISTEN_PORT = 5410


def _caller_ident(request: web.Request) -> str:
    """从 request 提取调用方标识（peer + User-Agent 前 60 字符），日志脱敏用。"""
    peer = request.remote or "unknown"
    ua = request.headers.get("User-Agent", "")[:60]
    return f"peer={peer} ua={ua!r}"


def _mask_body(body: Any) -> Any:
    """请求体摘要：敏感字段脱敏（token/server_url 仅记长度或占位），用于日志不泄露明文。"""
    if not isinstance(body, dict):
        return type(body).__name__
    masked = {}
    for k, v in body.items():
        key_lower = str(k).lower()
        if "token" in key_lower:
            # token 仅记长度，不打明文
            masked[k] = f"<token len={len(str(v)) if v else 0}>"
        elif "password" in key_lower or "secret" in key_lower:
            masked[k] = "<secret>"
        elif "cookie" in key_lower:
            masked[k] = f"<cookie len={len(str(v)) if v else 0}>"
        elif "url" in key_lower:
            # server_url 保留 scheme+host，去掉 path/query，避免泄露签名参数
            s = str(v)
            if s.startswith("http"):
                try:
                    from urllib.parse import urlparse
                    p = urlparse(s)
                    masked[k] = f"{p.scheme}://{p.netloc}/<masked_path>"
                except Exception:
                    masked[k] = "<url>"
            else:
                masked[k] = s[:80]
        elif isinstance(v, str) and len(v) > 100:
            masked[k] = f"<str len={len(v)}>"
        else:
            masked[k] = v
    return masked


class LocalApiServer:
    """本地控制 API 服务器。"""

    def __init__(self, core: Optional[SauAgentCore] = None) -> None:
        self._core = core
        self._app = web.Application(middlewares=[self._auth_middleware()])
        self._setup_routes()

    def set_core(self, core: SauAgentCore) -> None:
        """设置 Agent 核心引用（延迟注入，避免循环依赖）。"""
        self._core = core

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------
    async def run(self, stop_event: asyncio.Event) -> None:
        """启动 HTTP 服务器，直到 stop_event 被设置。"""
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, _LISTEN_HOST, _LISTEN_PORT)
        await site.start()
        logger.info("Local API server started on %s:%d", _LISTEN_HOST, _LISTEN_PORT)

        # 等待停止信号
        await stop_event.wait()
        await runner.cleanup()
        logger.info("Local API server stopped")

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------
    def _setup_routes(self) -> None:
        self._app.router.add_get("/status", self._handle_status)
        self._app.router.add_post("/login", self._handle_login)
        self._app.router.add_post("/accounts/recheck", self._handle_accounts_recheck)
        self._app.router.add_get("/accounts/status", self._handle_accounts_status)
        self._app.router.add_delete("/accounts", self._handle_accounts_delete)
        self._app.router.add_post("/config", self._handle_config_update)
        self._app.router.add_get("/config", self._handle_config_read)
        self._app.router.add_post("/reload", self._handle_reload)
        self._app.router.add_get("/upgrade", self._handle_upgrade)

    # ------------------------------------------------------------------
    # 认证中间件
    # ------------------------------------------------------------------
    def _auth_middleware(self):
        @web.middleware
        async def middleware(request: web.Request, handler):
            local_token = load_local_token()
            if not local_token:
                # _auth_fail warning 记 token 缺失：服务重启后 local_token 还没生成就被调用，
                # 典型场景是托盘启动快于服务，从日志能解释早期 5xx
                logger.warning(
                    "_auth_fail: local_token missing (not generated yet), path=%s %s",
                    request.path, _caller_ident(request),
                )
                return web.json_response({"error": "Local token not configured"}, status=500)

            req_token = request.headers.get("X-SAU-Local-Token", "")
            if req_token != local_token:
                # token 不匹配也是 auth_fail，但区分是缺失还是错误
                logger.warning(
                    "_auth_fail: token mismatch, path=%s %s",
                    request.path, _caller_ident(request),
                )
                return web.json_response({"error": "Unauthorized"}, status=401)

            return await handler(request)

        return middleware

    # ------------------------------------------------------------------
    # GET /status
    # ------------------------------------------------------------------
    async def _handle_status(self, request: web.Request) -> web.Response:
        """服务状态。"""
        path = request.path
        caller = _caller_ident(request)
        # debug：/status 会被托盘高频轮询，用 debug 不打满 info，
        # 需要排查连通性时把级别降到 debug 就能看到每次请求
        logger.debug("_handle_status: enter path=%s %s", path, caller)
        core = self._core
        account_list = accounts.scan()

        data: dict[str, Any] = {
            "service_running": True,
            "ws_connected": core.is_connected if core else False,
            "agent_id": load_config().get("agent_id", ""),
            "version": APP_VERSION,
            "active_tasks": core.dispatcher.active_count if core else 0,
            "accounts": account_list,
            "clock_offset_seconds": core.clock_offset_seconds if core else 0.0,
            "clock_sync_status": core.clock_sync_status if core else "unknown",
            # token 有效期（expire_at 为毫秒时间戳或 null，null=永久）
            "token_expire_at": core.token_expire_at if core else None,
            "token_remaining_days": core.token_remaining_days if core else None,
            "token_status": core.token_status if core else "unknown",
            # 最近一次关闭原因（bind_rejected 下发：replaced/rebind/token_reset，无则为 null）
            "last_close_reason": core.last_close_reason if core else None,
        }
        resp = web.json_response(data)
        # 每个 handler 开始时 info 记路径+调用方标识、请求体摘要、返回状态
        logger.info("handler path=%s method=%s %s body=None status=%d", path, request.method, caller, resp.status)
        return resp

    # ------------------------------------------------------------------
    # POST /login
    # ------------------------------------------------------------------
    async def _handle_login(self, request: web.Request) -> web.Response:
        """返回支持的平台列表。"""
        path = request.path
        caller = _caller_ident(request)
        try:
            raw_body = await request.json()
        except json.JSONDecodeError:
            raw_body = {}
        body_summary = _mask_body(raw_body)
        # _handle_login 开始+平台+账号 info：登录流程起点，能关联后续 upstream login 失败
        platform = raw_body.get("platform", "") if isinstance(raw_body, dict) else ""
        account = raw_body.get("account", "") if isinstance(raw_body, dict) else ""
        logger.info(
            "_handle_login: enter path=%s method=%s %s body=%s platform=%s account=%s",
            path, request.method, caller, body_summary, platform, account,
        )

        platforms = []
        for key, caps in PLATFORMS.items():
            platforms.append({
                "key": key,
                "supports_video": caps.video is not None,
                "supports_note": caps.note is not None,
            })
        resp = web.json_response({"platforms": platforms})
        # 登录成功/失败 info：/login 仅返回平台列表，这里统一按"成功枚举平台"记录；
        # 真正的登录结果在 sau_tray 侧 login_flows 里记录
        logger.info(
            "_handle_login: done path=%s status=%d platform_count=%d platform=%s account=%s",
            path, resp.status, len(platforms), platform, account,
        )
        # 每个 handler 统一记录返回状态
        logger.info("handler path=%s method=%s %s body=%s status=%d", path, request.method, caller, body_summary, resp.status)
        return resp

    # ------------------------------------------------------------------
    # POST /accounts/recheck
    # ------------------------------------------------------------------
    async def _handle_accounts_recheck(self, request: web.Request) -> web.Response:
        """触发全量 cookie 检查（后台异步执行，立即返回 task_id）。

        为什么改成异步：
        - 检查需要启动 patchright 浏览器，每个账号 ~10-25s，多账号串行会超过
          GUI 侧的 HTTP timeout（默认仅 10s），导致托盘菜单"账号检查失败: time out"。
        - 后台 Task 执行期间，调用方通过 GET /accounts/status?task_id=xxx 轮询拿结果，
          不会再有 HTTP 超时问题；本地 token header 相同不增加安全风险。
        """
        path = request.path
        caller = _caller_ident(request)
        try:
            raw_body = await request.json()
        except json.JSONDecodeError:
            raw_body = {}
        body_summary = _mask_body(raw_body)
        task_id = accounts.next_recheck_task_id()
        # 每个 handler 开始时 info
        logger.info("handler path=%s method=%s %s body=%s", path, request.method, caller, body_summary)
        # _handle_accounts_recheck 提交后台任务 info 记 task_id：
        # GUI 提交和后台 run_recheck_background 能通过 task_id 串起来
        logger.info(
            "_handle_accounts_recheck: submitted background task_id=%s %s",
            task_id, caller,
        )

        # 用 event_loop.create_task 挂后台，不 await —— handler 立即返回
        loop = asyncio.get_running_loop()
        loop.create_task(accounts.run_recheck_background(task_id))
        resp = web.json_response({
            "task_id": task_id,
            "status": "queued",
            "message": "账号检查已在后台开始，请通过 /accounts/status 轮询结果",
        })
        logger.info("handler path=%s method=%s %s body=%s status=%d task_id=%s", path, request.method, caller, body_summary, resp.status, task_id)
        return resp

    # ------------------------------------------------------------------
    # GET /accounts/status
    # ------------------------------------------------------------------
    async def _handle_accounts_status(self, request: web.Request) -> web.Response:
        """查询账号检查结果。

        - 若 ?task_id=xxx 存在且匹配 current_recheck.task_id：返回该任务进度/结果
        - 否则：
          ①有 last_check_all（上次检查缓存）→ 返回缓存的检查结果
          ②无缓存（从未检查过）→ 调用 accounts.scan() 扫出全部账号（含新旧两套
            体系），is_valid 置为 None 表示"未检查"，保证 GUI 至少能看到账号
            名称/平台，不会出现"扫到3个只显示2个"的一致性问题。
        """
        path = request.path
        caller = _caller_ident(request)
        task_id = request.query.get("task_id", "")
        # debug 带 task_id：/accounts/status 会被托盘轮询，debug 级别不污染 info，
        # 同时能看出轮询的是哪个 task_id，是否命中 current_recheck
        logger.debug("_handle_accounts_status: enter path=%s task_id=%s %s", path, task_id, caller)
        current = accounts.get_current_recheck()

        response: dict[str, Any] = {}
        if task_id and current is not None and current.task_id == task_id:
            response = {
                "task_id": current.task_id,
                "status": current.status,
                "started_at_ms": current.started_at_ms,
                "total": current.total,
                "done": current.done,
                "accounts": current.accounts,
                "error": current.error,
            }
        else:
            last = accounts.get_last_check_all()
            # checked_at_ms：get_last_check_all 本身只返回 list，用内部 _CHECKER 读时间
            # 为什么要读时间：GUI 展示"最后检查于 x 分钟前"，不打日志也要能通过 API 获取
            checked_at_ms = 0
            try:
                _, checked_at_ms = accounts._CHECKER.get_last_check_all()  # noqa: SLF001
            except Exception:
                checked_at_ms = 0
            if last is None:
                # 为什么在无缓存时也要扫账号：
                # 用户点"账号状态"弹框时，如果之前没点过"检查账号有效性"，
                # 接口不能直接返回空数组，否则会出现"Web端有3个但弹框只显示0/2个"
                # 的数据不一致错觉。这里直接 scan() 扫全量账号，is_valid=None 表示"未检查"。
                scanned = accounts.scan()
                accounts_view = []
                for acc in scanned:
                    accounts_view.append({
                        "platform_key": acc["platform_key"],
                        "account_name": acc["account_name"],
                        # None 表示尚未进行过有效性检查（不是失效，也不是有效）
                        "is_valid": None,
                        "checked_at": None,
                        "legacy_type": acc.get("legacy_type"),
                        "legacy_file": acc.get("legacy_file"),
                    })
                response = {
                    "status": "scanned_only",
                    "message": "尚未进行过账号检查，请先 POST /accounts/recheck 验证 cookie 有效性",
                    "accounts": accounts_view,
                }
            else:
                response = {
                    "status": "cached",
                    "accounts": last,
                    "checked_at_ms": checked_at_ms,
                }
        resp = web.json_response(response)
        logger.info("handler path=%s method=%s %s body=None status=%d task_id=%s", path, request.method, caller, resp.status, task_id)
        return resp

    # ------------------------------------------------------------------
    # DELETE /accounts
    # ------------------------------------------------------------------
    async def _handle_accounts_delete(self, request: web.Request) -> web.Response:
        """删除指定账号（新体系 cookie 文件 + 旧体系 SQLite + cookiesFile UUID 文件）。

        query:
            platform=<platform_key>（必填）
            account=<account_name>（必填）
        说明：
        - 新体系：删除 SAU_HOME/cookies/{platform}_{account}.json
        - 旧体系：同时删除 sau.db user_info 行 + SAU_HOME/cookiesFile/{uuid}.json
        - 删除完成后会自动触发 scan(emit_events=True) → Scanner diff 产出 removed 事件 →
          SauAgentCore 收到事件 → 立即 WS 上送 type=account_sync 给上游
        """
        path = request.path
        caller = _caller_ident(request)
        platform = request.query.get("platform", "")
        account = request.query.get("account", "")
        logger.info(
            "handler path=%s method=%s %s query=(platform=%s account=%s)",
            path, request.method, caller, platform, account,
        )
        if not platform or not account:
            resp = web.json_response({
                "error": "Missing required query params: platform and account",
            }, status=400)
            logger.info(
                "_handle_accounts_delete: 400 missing params path=%s status=%d",
                path, resp.status,
            )
            return resp
        ok, reason = accounts.delete_account(platform, account)
        if not ok:
            resp = web.json_response({
                "ok": False,
                "error": reason,
            }, status=404)
        else:
            resp = web.json_response({
                "ok": True,
                "platform": platform,
                "account": account,
            })
        logger.info(
            "_handle_accounts_delete: done path=%s status=%d ok=%s reason=%s",
            path, resp.status, ok, reason,
        )
        return resp

    # ------------------------------------------------------------------
    # POST /config
    # ------------------------------------------------------------------
    async def _handle_config_update(self, request: web.Request) -> web.Response:
        """更新 server_url / 绑定 token。"""
        path = request.path
        caller = _caller_ident(request)
        try:
            raw_body = await request.json()
            body = raw_body
        except json.JSONDecodeError:
            resp = web.json_response({"error": "Invalid JSON"}, status=400)
            logger.info("handler path=%s method=%s %s body=<invalid json> status=%d", path, request.method, caller, resp.status)
            return resp
        body_summary = _mask_body(body)
        # 每个 handler 开始时 info
        logger.info("handler path=%s method=%s %s body=%s", path, request.method, caller, body_summary)

        cfg = load_config()
        cfg_before = dict(cfg)
        changed = False

        if "server_url" in body:
            cfg["server_url"] = body["server_url"]
            changed = True

        if "agent_id" in body:
            cfg["agent_id"] = body["agent_id"]
            changed = True

        if "token" in body and body["token"]:
            save_token(body["token"])
            changed = True

        # 绑定场景：若提供了 server_url/token 但未提供 agent_id，自动生成
        if changed and "agent_id" not in body and ("server_url" in body or "token" in body):
            cfg["agent_id"] = get_agent_id()
            changed = True

        # _handle_config_update reload_config 前后 info，敏感字段占位
        before_agent_id = cfg_before.get("agent_id", "")[:8]
        after_agent_id = cfg.get("agent_id", "")[:8]
        logger.info(
            "_handle_config_update: reload before agent_id_prefix=%s changed=%s",
            before_agent_id, changed,
        )

        if changed:
            save_config(cfg)
            # 热重载
            if self._core:
                self._core.reload_config(cfg)

        # reload 后 info，敏感字段占位
        logger.info(
            "_handle_config_update: reload after agent_id_prefix=%s changed=%s",
            after_agent_id, changed,
        )

        resp = web.json_response({"status": "ok", "changed": changed})
        logger.info("handler path=%s method=%s %s body=%s status=%d changed=%s", path, request.method, caller, body_summary, resp.status, changed)
        return resp

    # ------------------------------------------------------------------
    # GET /config
    # ------------------------------------------------------------------
    async def _handle_config_read(self, request: web.Request) -> web.Response:
        """读取非敏感配置（token 只返回是否已绑定）。"""
        path = request.path
        caller = _caller_ident(request)
        cfg = load_config()
        token = load_token()
        resp = web.json_response({
            "server_url": cfg.get("server_url", ""),
            "agent_id": cfg.get("agent_id", ""),
            "heartbeat_interval": cfg.get("heartbeat_interval", 30),
            "max_concurrency": cfg.get("max_concurrency", 1),
            "token_bound": token is not None,
        })
        # 每个 handler 开始时 info 记路径+调用方、返回状态
        logger.info("handler path=%s method=%s %s body=None status=%d token_bound=%s", path, request.method, caller, resp.status, token is not None)
        return resp

    # ------------------------------------------------------------------
    # POST /reload
    # ------------------------------------------------------------------
    async def _handle_reload(self, request: web.Request) -> web.Response:
        """配置热重载（重连 WS）。"""
        path = request.path
        caller = _caller_ident(request)
        try:
            raw_body = await request.json()
        except json.JSONDecodeError:
            raw_body = {}
        body_summary = _mask_body(raw_body)
        # _handle_reload 开始 info：热重载是会断开 WS 的大动作，必须留痕
        logger.info("_handle_reload: start path=%s %s body=%s", path, caller, body_summary)
        cfg = load_config()
        if self._core:
            self._core.reload_config(cfg)
        resp = web.json_response({"status": "ok", "message": "Config reloaded, WS reconnecting"})
        # 结果 info：确认 reload 执行完成
        logger.info("_handle_reload: done path=%s status=%d", path, resp.status)
        logger.info("handler path=%s method=%s %s body=%s status=%d", path, request.method, caller, body_summary, resp.status)
        return resp

    # ------------------------------------------------------------------
    # GET /upgrade
    # ------------------------------------------------------------------
    async def _handle_upgrade(self, request: web.Request) -> web.Response:
        """返回更新状态（upgrade_state.json 内容；无状态时返回空对象）。"""
        path = request.path
        caller = _caller_ident(request)
        from sau_agent_pkg.updater import load_upgrade_state

        try:
            state = load_upgrade_state() or {}
        except Exception:
            logger.exception("Failed to read upgrade state")
            state = {}
        # debug 状态摘要：/upgrade 被托盘定时轮询，打 debug 不污染 info，
        # 需要排查升级流程时开 debug 就能看到 phase/version 变化
        phase = state.get("phase") if isinstance(state, dict) else None
        version = state.get("version") if isinstance(state, dict) else None
        logger.debug("_handle_upgrade: enter path=%s phase=%s version=%s %s", path, phase, version, caller)
        resp = web.json_response(state)
        logger.info("handler path=%s method=%s %s body=None status=%d phase=%s", path, request.method, caller, resp.status, phase)
        return resp
