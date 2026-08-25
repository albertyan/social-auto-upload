# -*- coding: utf-8 -*-
"""5409 本地 API（实施计划 S3；设计文档 §3.5 端点清单、§3.7 契约表、§4.4 端口决策）。

要点：
- 仅监听 ``127.0.0.1:5409``（端口可由 config.json ``local_api_port`` 覆盖，§4.4）；
- **绑定失败必须明确报错并记日志**（提示端口可能被上游遗留 ``sau_backend.py`` 或其他
  进程占用），禁止静默失败（§4.4）；服务主体（WS 主循环）不受影响继续运行；
- 鉴权：``X-SAU-Local-Token`` 头；令牌**服务每次启动重新生成**，写入
  ``%ProgramData%\\SAU\\local_token.bin``（users 可读，§3.6）；无令牌/错误令牌一律 401；
- 本步端点：``GET /status``、``GET/POST /config``、``POST /reload``、``POST /bind``；
  ``/ui/*``、登录/账号/升级端点族**占位 501**（控制台前端与升级留后续步骤，§6.4/§7.4）；
- 写操作审计：绑定/配置写入记一行审计日志（时间、操作、来源恒 127.0.0.1、结果）。
"""

from __future__ import annotations

import json
import logging
import secrets

from aiohttp import web

from sau_wrap import paths
from sau_wrap.agent import config as agent_config

#: 默认本地 API 端口（§4.4 定案；config.json 可覆盖）
DEFAULT_PORT = 5409

#: 占位端点（本步返回 501 + 说明，后续步骤实现）
_PLACEHOLDER_ROUTES = (
    ("GET", "/ui/{tail:.*}", "控制台静态托管（S6：§6.4 静态托管路由）"),
    ("GET", "/ui-ticket", "一次性令牌签发（S6：§6.3 鉴权链路）"),
    ("POST", "/login", "扫码登录会话（S5/S6：§6.5 登录会话族）"),
    ("POST", "/accounts/recheck", "账号状态复核（后续步骤）"),
    ("GET", "/accounts/status", "账号状态查询（后续步骤）"),
    ("DELETE", "/accounts", "账号删除（后续步骤）"),
    ("GET", "/upgrade", "升级状态快照（S7：§7.4）"),
    ("POST", "/upgrade/apply", "升级确认（S7：§7.4）"),
    ("POST", "/upgrade/snooze", "升级稍后提醒（S7：§7.4）"),
)


class LocalApiBindError(RuntimeError):
    """端口绑定失败（必须明确报错，禁止静默失败，§4.4）。"""


class LocalApiServer:
    """5409 本地 API 服务（与服务进程同进程，同一 asyncio 事件循环）。"""

    def __init__(self, logger: logging.Logger, ws_client, port: int = DEFAULT_PORT) -> None:
        self._logger = logger
        self._client = ws_client
        self.port = port
        self.token: str = ""
        self._runner: web.AppRunner | None = None

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

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._logger.info("本地 API 已停止")

    def _regenerate_token(self) -> None:
        """服务每次启动重新生成令牌（§3.6），写入 local_token.bin（users 可读）。"""
        self.token = secrets.token_urlsafe(32)
        paths.ensure_dir(paths.DATA_ROOT)
        paths.LOCAL_TOKEN_FILE.write_bytes(self.token.encode("utf-8"))

    # ------------------------------------------------------------ 鉴权

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        """鉴权中间件（aiohttp 3.14 需 @web.middleware 装饰）。"""
        supplied = request.headers.get("X-SAU-Local-Token", "")
        if not supplied or not secrets.compare_digest(supplied, self.token):
            return web.json_response(
                {"error": "unauthorized", "message": "缺少或错误的 X-SAU-Local-Token"},
                status=401,
            )
        return await handler(request)

    # ------------------------------------------------------------ 路由

    def _register_routes(self, app: web.Application) -> None:
        app.router.add_get("/status", self._get_status)
        app.router.add_get("/config", self._get_config)
        app.router.add_post("/config", self._post_config)
        app.router.add_post("/reload", self._post_reload)
        app.router.add_post("/bind", self._post_bind)
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

    # ------------------------------------------------------------ 端点实现

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
            self._audit("config", "fail", "未绑定（请先 POST /bind 或 sau bind）")
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
        self._audit("config", "success", f"server_url={server_url}")
        return web.json_response({
            "ok": True, "reload": reload_msg + port_note,
            "server_url": cfg.server_url,
        })

    async def _post_reload(self, request: web.Request) -> web.Response:
        """``POST /reload``：热重载——唤醒挂起态/断开当前连接以新配置重连。"""
        reload_msg = self._client.trigger_reload()
        self._audit("reload", "success", "-")
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
            self._audit("bind", "fail", str(exc))
            return web.json_response({"error": "bind_failed", "message": str(exc)}, status=400)
        reload_msg = self._client.trigger_reload()
        self._audit("bind", "success", f"server_url={cfg.server_url} agent_id={cfg.agent_id}")
        return web.json_response({
            "ok": True, "server_url": cfg.server_url, "agent_id": cfg.agent_id,
            "reload": reload_msg,
        })

    # ------------------------------------------------------------ 审计

    def _audit(self, operation: str, result: str, detail: str) -> None:
        """写操作审计日志（§任务要求：时间由日志格式承载；来源恒 127.0.0.1）。"""
        self._logger.info(
            "[AUDIT] op=%s source=127.0.0.1 result=%s detail=%s",
            operation, result, detail,
        )
