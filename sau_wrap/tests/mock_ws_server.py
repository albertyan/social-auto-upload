# -*- coding: utf-8 -*-
"""Mock WS 服务端（任务 #13 第二步 / S2 本地验证专用，不依赖 opcgeo 后端）。

模拟服务端行为：
- 记录收到的全部上行消息（register/heartbeat/task_result…）；
- register → 回 ``registered``；
- heartbeat → 回 ``heartbeat_ack``（server_time=当前毫秒）；
- 可编程动作：主动下发 ``publish_task``、以指定关闭码踢掉当前会话
  （模拟 4401/4403/4409/4410 与异常断线 1011）。

仅用于 ``sau_wrap/tests/verify_s2.py`` 的场景验证，禁止用于生产。
"""

from __future__ import annotations

import asyncio
import json
import time

from websockets.asyncio.server import serve


def _msg(msg_type: str, data: dict) -> str:
    return json.dumps({"type": msg_type, "data": data}, ensure_ascii=False)


class MockAgentServer:
    """可编程 mock 服务端。``events`` 记录 (时刻, 事件名, 附加信息)。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host = host
        self.port = port
        self.received: list[dict] = []      # 全部上行消息
        self.events: list[tuple[float, str, str]] = []  # (t, event, info)
        self.connections = 0
        #: 每个新会话建立时依次弹出的动作（"close:<code>" / "publish:<task_id>" / 其他忽略）
        self.on_connect_actions: list[str] = []
        self._server = None
        self._current_ws = None

    # ------------------------------------------------ 生命周期

    async def start(self) -> int:
        self._server = await serve(self._handler, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}/opcgeo/agent/ws"

    # ------------------------------------------------ 处理

    def _log_event(self, event: str, info: str = "") -> None:
        self.events.append((time.monotonic(), event, info))

    async def _handler(self, ws) -> None:
        self.connections += 1
        self._current_ws = ws
        self._log_event("connect", f"#{self.connections} path={ws.request.path}")
        # 连接建立动作（模拟服务端主动行为）
        while self.on_connect_actions:
            action = self.on_connect_actions.pop(0)
            if action.startswith("close:"):
                code = int(action.split(":", 1)[1])
                self._log_event("server_close", f"code={code}")
                await ws.close(code, f"mock close {code}")
                return
            if action.startswith("publish:"):
                task_id = action.split(":", 1)[1]
                await ws.send(_msg("publish_task", {
                    "task_id": task_id,
                    "platform_key": "douyin",
                    "content_type": "video",
                    "file_url": "https://example.invalid/mock.mp4",
                    "media_urls": [],
                    "title": "mock 任务标题",
                    "description": "mock 描述",
                    "tags": ["mock"],
                    "account_name": "mock-account",
                    "submit_mode": "auto",
                    "scheduled_at": int(time.time() * 1000),
                }))
                self._log_event("server_publish", task_id)
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                self.received.append(msg)
                msg_type = msg.get("type")
                self._log_event("recv", msg_type or "?")
                if msg_type == "register":
                    await ws.send(_msg("registered", {
                        "agent_id": msg["data"].get("agent_id"),
                        "machine_bound": True,
                        "expire_at": None,
                        "pending_tasks": [],
                    }))
                elif msg_type == "heartbeat":
                    await ws.send(_msg("heartbeat_ack", {
                        "server_time": int(time.time() * 1000),
                        "expire_at": None,
                    }))
                # task_result / 其他：仅记录（服务端幂等回写不在 mock 范围）
        except Exception:
            pass
        self._log_event("disconnect", f"conn#{self.connections}")

    # ------------------------------------------------ 控制

    async def close_current(self, code: int, reason: str = "mock") -> None:
        """踢掉当前会话（模拟凭证类关闭码 / 异常）。"""
        ws = self._current_ws
        if ws is not None:
            self._log_event("server_close", f"code={code}")
            await ws.close(code, reason)

    def received_types(self) -> list[str]:
        return [m.get("type", "?") for m in self.received]

    def received_of(self, msg_type: str) -> list[dict]:
        return [m for m in self.received if m.get("type") == msg_type]
