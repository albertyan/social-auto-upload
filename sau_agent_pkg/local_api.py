"""
sau_agent_pkg.local_api
~~~~~~~~~~~~~~~~~~~~~~~
本地控制 HTTP API 服务器（aiohttp.web）。

监听 127.0.0.1:5410，所有请求需校验 X-SAU-Local-Token header。

接口：
- GET  /status            服务状态
- POST /login             返回支持的平台列表
- POST /accounts/recheck  触发全量 cookie 检查
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
                return web.json_response({"error": "Local token not configured"}, status=500)

            req_token = request.headers.get("X-SAU-Local-Token", "")
            if req_token != local_token:
                return web.json_response({"error": "Unauthorized"}, status=401)

            return await handler(request)

        return middleware

    # ------------------------------------------------------------------
    # GET /status
    # ------------------------------------------------------------------
    async def _handle_status(self, request: web.Request) -> web.Response:
        """服务状态。"""
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
        return web.json_response(data)

    # ------------------------------------------------------------------
    # POST /login
    # ------------------------------------------------------------------
    async def _handle_login(self, request: web.Request) -> web.Response:
        """返回支持的平台列表。"""
        platforms = []
        for key, caps in PLATFORMS.items():
            platforms.append({
                "key": key,
                "supports_video": caps.video is not None,
                "supports_note": caps.note is not None,
            })
        return web.json_response({"platforms": platforms})

    # ------------------------------------------------------------------
    # POST /accounts/recheck
    # ------------------------------------------------------------------
    async def _handle_accounts_recheck(self, request: web.Request) -> web.Response:
        """触发全量 cookie 检查。"""
        results = await accounts.check_all()
        return web.json_response({"accounts": results})

    # ------------------------------------------------------------------
    # POST /config
    # ------------------------------------------------------------------
    async def _handle_config_update(self, request: web.Request) -> web.Response:
        """更新 server_url / 绑定 token。"""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        cfg = load_config()
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

        if changed:
            save_config(cfg)
            # 热重载
            if self._core:
                self._core.reload_config(cfg)

        return web.json_response({"status": "ok", "changed": changed})

    # ------------------------------------------------------------------
    # GET /config
    # ------------------------------------------------------------------
    async def _handle_config_read(self, request: web.Request) -> web.Response:
        """读取非敏感配置（token 只返回是否已绑定）。"""
        cfg = load_config()
        token = load_token()
        return web.json_response({
            "server_url": cfg.get("server_url", ""),
            "agent_id": cfg.get("agent_id", ""),
            "heartbeat_interval": cfg.get("heartbeat_interval", 30),
            "max_concurrency": cfg.get("max_concurrency", 1),
            "token_bound": token is not None,
        })

    # ------------------------------------------------------------------
    # POST /reload
    # ------------------------------------------------------------------
    async def _handle_reload(self, request: web.Request) -> web.Response:
        """配置热重载（重连 WS）。"""
        cfg = load_config()
        if self._core:
            self._core.reload_config(cfg)
        return web.json_response({"status": "ok", "message": "Config reloaded, WS reconnecting"})

    # ------------------------------------------------------------------
    # GET /upgrade
    # ------------------------------------------------------------------
    async def _handle_upgrade(self, request: web.Request) -> web.Response:
        """返回更新状态（upgrade_state.json 内容；无状态时返回空对象）。"""
        from sau_agent_pkg.updater import load_upgrade_state

        try:
            state = load_upgrade_state() or {}
        except Exception:
            logger.exception("Failed to read upgrade state")
            state = {}
        return web.json_response(state)
