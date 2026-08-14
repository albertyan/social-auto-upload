"""
sau_agent_pkg.core
~~~~~~~~~~~~~~~~~~
WS 主循环：连接/注册/心跳/消息路由/时钟同步/断线补发。

职责：
- WebSocket 长连接 opcgeo 服务端
- 发送 register 消息（agent_id, machine_code, version, platforms, accounts）
- 心跳协程（每 30s）
- 消息路由：publish_task / account_check / heartbeat_ack / file_renewed / bind_rejected / upgrade_notice
- 指数退避重连（2s → 4s → ... → 300s 上限）
- 关闭码处理：4409/4401/4403/4410（凭证类）挂起等待新凭证（reload_config 唤醒），挂起期间零重连
- 时钟同步：滑动平均 clock_offset，偏差 >5min 暂停调度
- token 有效期：registered/heartbeat_ack 同步 expire_at，超宽限期冻结调度
- result_queue 断线补发
- ERROR 日志上报（60s 去重 + 脱敏）
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed

# 为什么要显式 import websockets.asyncio.client：
# websockets 12.x 开始把 connect() 等 API 搬到了 websockets.asyncio.client，
# 顶层的 websockets.connect() 实际是通过 websockets/imports.py 中的
# 动态 import_name("websockets.asyncio.client") 进行懒加载的。
# Nuitka 的静态依赖扫描无法追踪这种运行时字符串导入，会把 websockets/asyncio
# 整个目录从打包产物中漏掉，导致打包后报 ModuleNotFoundError: No module named 'websockets.asyncio'。
# 这里加一条显式 import 后，Nuitka 会静态识别并强制收集 websockets.asyncio.* 全部子模块。
import websockets.asyncio.client  # noqa: F401 （此 import 用于打包依赖收集，不直接使用）

from sau_agent_pkg import accounts
from sau_agent_pkg.config import SAU_HOME, load_config, load_token, save_config, save_token
from sau_agent_pkg.db_init import get_connection
from sau_agent_pkg.dispatcher import Dispatcher
from sau_agent_pkg.machine import get_machine_code
from sau_agent_pkg.upstream_adapter import PLATFORMS
from sau_agent_pkg.version import APP_VERSION

logger = logging.getLogger(__name__)

CLOCK_DRIFT_THRESHOLD = timedelta(minutes=5)
_BACKOFF_INITIAL = 2
_BACKOFF_MAX = 300
_HEARTBEAT_INTERVAL = 30
_SLIDING_WINDOW_SIZE = 10

# token 有效期相关（与服务端契约一致：宽限期为到期后 3 天）
_TOKEN_GRACE_MS = 3 * 24 * 3600 * 1000       # 宽限期：到期后 3 天
_TOKEN_EXPIRING_MS = 7 * 24 * 3600 * 1000    # 即将到期告警阈值：剩余 ≤7 天
_MACHINE_CODE_RETRY_SECONDS = 60               # 机器指纹采集失败重试间隔

# 脱敏正则
_SENSITIVE_PATTERNS = [
    re.compile(r'(token["\s:=]+)["\']?[^"\'}\s,]+', re.IGNORECASE),
    re.compile(r'(cookie["\s:=]+)["\']?[^"\'}\s,]+', re.IGNORECASE),
    re.compile(r'(password["\s:=]+)["\']?[^"\'}\s,]+', re.IGNORECASE),
    re.compile(r'(secret["\s:=]+)["\']?[^"\'}\s,]+', re.IGNORECASE),
    re.compile(r'Bearer\s+[A-Za-z0-9._\-]+', re.IGNORECASE),
]


def _sanitize(text: str) -> str:
    """对日志消息进行脱敏处理。"""
    result = text
    for pattern in _SENSITIVE_PATTERNS:
        result = pattern.sub(r'\1***', result)
    return result


class SauAgentCore:
    """SAU Agent 核心：WS 主循环。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or load_config()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._connected = False
        self._stop_event: Optional[asyncio.Event] = None
        # 凭证失效后的恢复事件：run() 中惰性创建（同 _stop_event，避免在无 loop 环境创建）
        self._resume_event: Optional[asyncio.Event] = None

        # 时钟同步
        self._offset_samples: deque[int] = deque(maxlen=_SLIDING_WINDOW_SIZE)
        self._clock_offset_ms: int = 0
        self._clock_drifted = False

        # token 有效期（服务端下发，毫秒时间戳，None 表示永久）
        self._token_expire_at: Optional[int] = None
        # token 已过期（超过宽限期）冻结标记：只入队不执行，同 clock_drifted 语义
        self._token_expired = False

        # 最近一次 bind_rejected 下发的关闭原因（replaced/rebind/token_reset），用于 4403 语义细化
        self._last_close_reason: Optional[str] = None

        # 调度器
        max_concurrency = self._config.get("max_concurrency", 1)
        self._dispatcher = Dispatcher(
            max_concurrency=max_concurrency,
            progress_callback=self.send_task_progress,
            result_callback=self.send_task_result,
            file_renew_callback=self._request_file_renew,
        )

        # ERROR 日志去重
        self._error_dedup: dict[str, float] = {}
        self._error_dedup_ttl = 60  # 秒

        # result_queue 并发访问保护
        self._result_queue_lock = asyncio.Lock()

        # 升级通知回调（外部可设置）
        self.on_upgrade_notice: Optional[Any] = None
        # 连接状态变更回调
        self.on_connection_change: Optional[Any] = None

    @property
    def dispatcher(self) -> Dispatcher:
        return self._dispatcher

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def clock_offset_seconds(self) -> float:
        return self._clock_offset_ms / 1000.0

    @property
    def clock_sync_status(self) -> str:
        return "drifted" if self._clock_drifted else "normal"

    @property
    def last_close_reason(self) -> Optional[str]:
        """最近一次 bind_rejected 下发的关闭原因（replaced/rebind/token_reset），无则为 None。"""
        return self._last_close_reason

    # ------------------------------------------------------------------
    # token 有效期（只读属性，供 local_api /status 使用）
    # ------------------------------------------------------------------
    def _now_corrected_ms(self) -> int:
        """基于服务端时钟校正后的当前时间（毫秒）。"""
        local_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        return local_ms + self._clock_offset_ms

    @property
    def token_expire_at(self) -> Optional[int]:
        """token 到期时间（毫秒时间戳），None 表示永久或未同步。"""
        return self._token_expire_at

    @property
    def token_remaining_days(self) -> Optional[float]:
        """token 剩余天数（基于服务端校正时间）；expire_at 为 None 返回 None（永久）。"""
        if self._token_expire_at is None:
            return None
        remaining_ms = self._token_expire_at - self._now_corrected_ms()
        return remaining_ms / (24 * 3600 * 1000)

    @property
    def token_status(self) -> str:
        """
        token 状态：
        - "permanent"：永久有效（expire_at 为 None）
        - "normal"：正常（剩余 >7 天）
        - "expiring"：即将到期（剩余 ≤7 天）
        - "grace"：已到期但处于宽限期（到期后 3 天内）
        - "expired"：超过宽限期
        """
        if self._token_expire_at is None:
            return "permanent"
        remaining_ms = self._token_expire_at - self._now_corrected_ms()
        if remaining_ms <= -_TOKEN_GRACE_MS:
            return "expired"
        if remaining_ms <= 0:
            return "grace"
        if remaining_ms <= _TOKEN_EXPIRING_MS:
            return "expiring"
        return "normal"

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    async def run(self, stop_event: asyncio.Event) -> None:
        """WS 主循环：连接 → 注册 → 收消息 → 重连。"""
        self._stop_event = stop_event
        self._resume_event = asyncio.Event()
        backoff = _BACKOFF_INITIAL

        while not stop_event.is_set():
            token = load_token()
            if not token:
                logger.debug("No token bound, waiting for binding...")
                await asyncio.sleep(30)
                continue

            server_url = self._config.get("server_url", "")
            agent_id = self._config.get("agent_id", "")
            try:
                machine_code = get_machine_code()
            except Exception as e:
                # 机器指纹采集失败：记录 ERROR 并上报，等待后重试（不退出主循环）
                logger.error("Machine code collection failed: %s", e)
                await self.report_error("machine", f"机器指纹采集失败: {e}")
                if stop_event.is_set():
                    break
                await asyncio.sleep(_MACHINE_CODE_RETRY_SECONDS)
                continue
            # token 通过 Authorization header 传递，不再放在 URL query 中
            url = f"{server_url}?agentId={agent_id}&machine={machine_code}"

            try:
                async with websockets.connect(
                    url,
                    ping_interval=None,
                    additional_headers={"Authorization": f"Bearer {token}"},
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._notify_connection(True)
                    logger.info("Connected to %s", server_url)

                    # 发送 register
                    await self._send_register(ws, agent_id, machine_code)
                    backoff = _BACKOFF_INITIAL  # 连上即重置退避
                    self._last_close_reason = None  # 连上即清除上次关闭原因

                    # 启动心跳协程
                    heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
                    # 启动 result_queue 补发
                    replay_task = asyncio.create_task(self._replay_result_queue(ws))

                    try:
                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                                await self._route_message(ws, msg)
                            except json.JSONDecodeError:
                                logger.warning("Invalid JSON message: %s", raw[:200])
                    finally:
                        heartbeat_task.cancel()
                        replay_task.cancel()

            except ConnectionClosed as e:
                logger.warning("WS connection closed: code=%s reason=%s", e.code, e.reason)
                self._connected = False
                self._ws = None
                self._notify_connection(False)

                if e.code == 4409:
                    logger.error("Machine code mismatch (4409). Suspending reconnect until new credential.")
                    if await self._wait_for_resume(stop_event):
                        break
                    backoff = _BACKOFF_INITIAL  # 凭证更新后重置退避
                    continue
                if e.code == 4403:
                    if self._last_close_reason == "replaced":
                        logger.error("该账号已在其他机器连接，本机已下线，挂起等待新凭证 (4403)")
                    elif self._last_close_reason == "rebind":
                        logger.error("服务端已换机绑定，本机凭证失效，挂起等待新凭证 (4403)")
                    elif self._last_close_reason == "token_reset":
                        logger.error("服务端已重置 Token，本机凭证失效，挂起等待新凭证 (4403)")
                    elif self._last_close_reason == "disabled":
                        logger.error("Agent disabled by server, suspending reconnect until re-enabled (4403)")
                    elif self._last_close_reason == "deleted":
                        logger.error("Agent deleted on server, suspending reconnect until re-bound (4403)")
                    else:
                        logger.error("Credential invalid (4403). Suspending reconnect until new credential.")
                    if await self._wait_for_resume(stop_event):
                        break
                    backoff = _BACKOFF_INITIAL  # 凭证更新后重置退避
                    continue
                if e.code == 4401:
                    logger.error("Credential invalid (4401). Suspending reconnect until new credential.")
                    if await self._wait_for_resume(stop_event):
                        break
                    backoff = _BACKOFF_INITIAL  # 凭证更新后重置退避
                    continue
                if e.code == 4410:
                    logger.error("Token expired (4410), please contact admin to renew. Suspending reconnect until new credential.")
                    self._token_expired = True
                    self._dispatcher.pause()
                    if await self._wait_for_resume(stop_event):
                        break
                    # 能被唤醒说明凭证已更新（服务端续期/重置后重新绑定）：
                    # 连接成功路径无 _token_expired 清除逻辑，须在此清除，否则重连后调度仍暂停
                    self._token_expired = False
                    if not self._clock_drifted:
                        self._dispatcher.resume()
                    backoff = _BACKOFF_INITIAL  # 凭证更新后重置退避
                    continue

            except Exception:
                logger.exception("WS connection error")
                self._connected = False
                self._ws = None
                self._notify_connection(False)

            if stop_event.is_set():
                break

            logger.info("Reconnecting in %ds...", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    # ------------------------------------------------------------------
    # 凭证失效挂起
    # ------------------------------------------------------------------
    async def _wait_for_resume(self, stop_event: asyncio.Event) -> bool:
        """凭证失效后挂起等待新凭证（reload_config 唤醒）或停止信号。

        挂起期间零重连尝试（防止互踢风暴）。返回 True 表示应退出主循环（stop 触发）。
        """
        logger.info("Suspended: waiting for new credential (POST /config) or stop...")
        resume_task = asyncio.create_task(self._resume_event.wait())
        stop_task = asyncio.create_task(stop_event.wait())
        try:
            await asyncio.wait(
                {resume_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (resume_task, stop_task):
                task.cancel()
        if stop_event.is_set():
            return True
        self._resume_event.clear()
        logger.info("Credential updated, resuming connection")
        return False

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    async def _send_register(self, ws: websockets.WebSocketClientProtocol, agent_id: str, machine_code: str) -> None:
        """发送 register 消息。"""
        msg = {
            "type": "register",
            "data": {
                "agent_id": agent_id,
                "machine_code": machine_code,
                "version": APP_VERSION,
                "platforms": list(PLATFORMS.keys()),
                "accounts": accounts.scan(),
            },
        }
        await ws.send(json.dumps(msg, ensure_ascii=False))
        logger.info("Register message sent")

    # ------------------------------------------------------------------
    # 心跳
    # ------------------------------------------------------------------
    async def _heartbeat_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """每 30s 发送心跳。"""
        interval = self._config.get("heartbeat_interval", _HEARTBEAT_INTERVAL)
        while True:
            await asyncio.sleep(interval)
            try:
                msg = {
                    "type": "heartbeat",
                    "data": {
                        "agent_id": self._config.get("agent_id", ""),
                        "active_tasks": self._dispatcher.active_count,
                        "accounts": accounts.scan(),
                        "clock_offset_seconds": self.clock_offset_seconds,
                    },
                }
                await ws.send(json.dumps(msg, ensure_ascii=False))
            except Exception:
                logger.debug("Heartbeat send failed")
                break

    # ------------------------------------------------------------------
    # 消息路由
    # ------------------------------------------------------------------
    async def _route_message(self, ws: websockets.WebSocketClientProtocol, msg: dict) -> None:
        """路由收到的消息。"""
        msg_type = msg.get("type", "")
        data = msg.get("data", {})

        handlers = {
            "registered": self._handle_registered,
            "publish_task": self._handle_publish_task,
            "account_check": self._handle_account_check,
            "heartbeat_ack": self._handle_heartbeat_ack,
            "file_renewed": self._handle_file_renewed,
            "bind_rejected": self._handle_bind_rejected,
            "upgrade_notice": self._handle_upgrade_notice,
            "token_expired": self._handle_token_expired,
        }

        handler = handlers.get(msg_type)
        if handler:
            await handler(ws, data)
        else:
            logger.debug("Unknown message type: %s", msg_type)

    async def _handle_registered(self, ws: websockets.WebSocketClientProtocol, data: dict) -> None:
        """处理 registered 消息：注册成功。"""
        logger.info("Registered: %s", data.get("message", ""))

        # 缓存 token 有效期（毫秒或 null，null=永久）
        if "expire_at" in data:
            self._token_expire_at = data.get("expire_at")
            logger.info("Token expire_at synced: %s", self._token_expire_at or "permanent")
            self._apply_token_expiry()

        # 服务端权威 agent_id：与本地不一致时写回 config.json
        server_agent_id = data.get("agent_id", "")
        local_agent_id = self._config.get("agent_id", "")
        if server_agent_id and server_agent_id != local_agent_id:
            try:
                cfg = load_config()
                cfg["agent_id"] = server_agent_id
                save_config(cfg)
                self._config["agent_id"] = server_agent_id
                logger.info("Agent ID updated from server: %s -> %s", local_agent_id, server_agent_id)
            except Exception:
                logger.exception("Failed to persist authoritative agent_id")

        # 处理 pending_tasks（离线期间积压任务）；or [] 防御服务端发 null
        pending = data.get("pending_tasks") or []
        for task in pending:
            self._dispatcher.submit(task)
            logger.info("Submitted pending task: %s", task.get("task_id"))

    async def _handle_publish_task(self, ws: websockets.WebSocketClientProtocol, data: dict) -> None:
        """处理 publish_task：提交给调度器。"""
        task_id = data.get("task_id", "unknown")
        if self._clock_drifted or self._token_expired:
            # 时钟偏差过大或 token 已过期，只入队不执行
            reason = "clock drifted" if self._clock_drifted else "token expired"
            logger.warning("%s, task %s queued but not executed", reason.capitalize(), task_id)
            self._dispatcher.submit(data)
        else:
            self._dispatcher.submit(data)
        logger.info("Received publish_task: %s", task_id)

    async def _handle_account_check(self, ws: websockets.WebSocketClientProtocol, data: dict) -> None:
        """处理 account_check：全量检查并上报。"""
        results = await accounts.check_all()
        msg = {"type": "account_sync", "data": {"accounts": results}}
        await ws.send(json.dumps(msg, ensure_ascii=False))
        logger.info("Account check completed, %d accounts reported", len(results))

    async def _handle_heartbeat_ack(self, ws: websockets.WebSocketClientProtocol, data: dict) -> None:
        """处理 heartbeat_ack：时钟同步 + token 有效期同步。"""
        server_time_ms = data.get("server_time")
        if server_time_ms is not None:
            local_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            offset_ms = server_time_ms - local_ms
            self._offset_samples.append(offset_ms)
            self._clock_offset_ms = sum(self._offset_samples) // len(self._offset_samples)

            offset_seconds = self._clock_offset_ms / 1000.0
            if abs(offset_seconds) > CLOCK_DRIFT_THRESHOLD.total_seconds():
                if not self._clock_drifted:
                    self._clock_drifted = True
                    self._dispatcher.pause()
                    logger.warning("Clock drift detected: %.1fs, dispatcher paused", offset_seconds)
            elif self._clock_drifted and abs(offset_seconds) <= CLOCK_DRIFT_THRESHOLD.total_seconds():
                self._clock_drifted = False
                if not self._token_expired:
                    self._dispatcher.resume()
                logger.info("Clock drift recovered: %.1fs, dispatcher resumed", offset_seconds)

        # token 有效期同步（heartbeat_ack 下发 expire_at / expired_warning）
        if "expire_at" in data:
            self._token_expire_at = data.get("expire_at")
        expired_warning = bool(data.get("expired_warning", False))
        self._apply_token_expiry(expired_warning=expired_warning)

    def _apply_token_expiry(self, expired_warning: bool = False) -> None:
        """
        基于（服务端校正后的）当前时间判断 token 有效期状态：
        - 超过 expire_at + 3 天宽限期 → 过期冻结（同 clock_drifted 语义：pause，只入队不执行）
        - 宽限期内 → 告警日志
        - 服务端续期（expire_at 恢复未来）→ 自动恢复 resume
        """
        expire_at = self._token_expire_at
        if expire_at is None:
            # 永久（或服务端续期为永久）：若曾冻结则恢复
            if self._token_expired:
                self._token_expired = False
                if not self._clock_drifted:
                    self._dispatcher.resume()
                logger.info("Token renewed to permanent, dispatcher resumed")
            return

        now_ms = self._now_corrected_ms()
        remaining_ms = expire_at - now_ms

        if remaining_ms <= -_TOKEN_GRACE_MS:
            # 超过宽限期：冻结调度
            if not self._token_expired:
                self._token_expired = True
                self._dispatcher.pause()
                logger.error("Token expired beyond grace period, dispatcher paused")
        elif remaining_ms <= 0:
            # 宽限期内：告警
            logger.warning(
                "Token expired, within grace period (expired_warning=%s), please renew ASAP",
                expired_warning,
            )
        else:
            # 未过期：若曾冻结（服务端续期）则自动恢复
            if self._token_expired:
                self._token_expired = False
                if not self._clock_drifted:
                    self._dispatcher.resume()
                logger.info("Token renewed, dispatcher resumed")
            elif remaining_ms <= _TOKEN_EXPIRING_MS:
                logger.warning(
                    "Token expiring soon: %.1f days remaining", remaining_ms / (24 * 3600 * 1000)
                )

    async def _handle_token_expired(self, ws: websockets.WebSocketClientProtocol, data: dict) -> None:
        """处理 token_expired：服务端通知 token 过期（随后连接会以 4410 关闭）。"""
        message = data.get("message", "")
        logger.error("Server notified token expired: %s (connection will be closed with 4410)", message)

    async def _handle_file_renewed(self, ws: websockets.WebSocketClientProtocol, data: dict) -> None:
        """处理 file_renewed：素材重签完成，重新提交任务。"""
        task_id = data.get("task_id", "")
        logger.info("File renewed for task %s, re-submitting", task_id)
        # 更新任务中的 URL 并重新提交
        conn = get_connection()
        try:
            row = conn.execute("SELECT payload FROM local_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row:
                task = json.loads(row["payload"])
                if "file_url" in data and data["file_url"]:
                    task["file_url"] = data["file_url"]
                if "media_urls" in data and data["media_urls"]:
                    task["media_urls"] = data["media_urls"]
                self._dispatcher.submit(task)
        finally:
            conn.close()

    async def _handle_bind_rejected(self, ws: websockets.WebSocketClientProtocol, data: dict) -> None:
        """处理 bind_rejected：拒绝绑定。"""
        reason = data.get("reason", "unknown")
        message = data.get("message", "")
        self._last_close_reason = reason
        if reason == "replaced":
            logger.warning("Session replaced by a new connection on another machine, will stop reconnecting")
        elif reason == "rebind":
            logger.warning("Server rebind detected (bound on another machine), local credential invalidated, will stop reconnecting")
        elif reason == "token_reset":
            logger.warning("Server token reset detected, local credential invalidated, will stop reconnecting")
        elif reason == "disabled":
            logger.warning("Agent disabled by server, will stop reconnecting")
        elif reason == "deleted":
            logger.warning("Agent deleted on server, will stop reconnecting")
        else:
            logger.error("Bind rejected: reason=%s, message=%s", reason, message)
        # 连接将被服务端关闭，在 ConnectionClosed 中处理

    async def _handle_upgrade_notice(self, ws: websockets.WebSocketClientProtocol, data: dict) -> None:
        """处理 upgrade_notice：升级提醒。"""
        version = data.get("version", "")
        logger.info("Upgrade notice: version=%s", version)
        if self.on_upgrade_notice:
            try:
                await self.on_upgrade_notice(data) if asyncio.iscoroutinefunction(self.on_upgrade_notice) else self.on_upgrade_notice(data)
            except Exception:
                logger.exception("Error in upgrade notice callback")

    # ------------------------------------------------------------------
    # 回调：供 dispatcher 使用
    # ------------------------------------------------------------------
    async def send_task_progress(self, task_id: str, stage: str, percent: int, message: str) -> None:
        """发送 task_progress 消息。"""
        if not self._ws:
            return
        msg = {
            "type": "task_progress",
            "data": {
                "task_id": task_id,
                "stage": stage,
                "percent": percent,
                "message": message,
            },
        }
        try:
            await self._ws.send(json.dumps(msg, ensure_ascii=False))
        except Exception:
            logger.debug("Failed to send task_progress for %s", task_id)

    async def send_task_result(self, task_id: str, status: str, error: str | None, publish_url: str | None) -> None:
        """发送 task_result 消息，同时落 result_queue。"""
        result_data = {
            "task_id": task_id,
            "status": status,
            "error": error,
            "publish_url": publish_url,
        }
        async with self._result_queue_lock:
            # 落 result_queue
            conn = get_connection()
            try:
                conn.execute(
                    "INSERT INTO result_queue (task_id, payload) VALUES (?, ?)",
                    (task_id, json.dumps(result_data, ensure_ascii=False)),
                )
                conn.commit()
            finally:
                conn.close()

            # 尝试通过 WS 发送
            if self._ws:
                msg = {"type": "task_result", "data": result_data}
                try:
                    await self._ws.send(json.dumps(msg, ensure_ascii=False))
                    # 发送成功，从队列中移除
                    conn = get_connection()
                    try:
                        conn.execute("DELETE FROM result_queue WHERE task_id = ?", (task_id,))
                        conn.commit()
                    finally:
                        conn.close()
                except Exception:
                    logger.debug("Failed to send task_result for %s, kept in queue", task_id)

    async def _request_file_renew(self, task_id: str) -> None:
        """请求素材重签。"""
        if not self._ws:
            return
        msg = {"type": "file_renew", "data": {"task_id": task_id}}
        try:
            await self._ws.send(json.dumps(msg, ensure_ascii=False))
        except Exception:
            logger.debug("Failed to send file_renew for %s", task_id)

    # ------------------------------------------------------------------
    # result_queue 补发
    # ------------------------------------------------------------------
    async def _replay_result_queue(self, ws: websockets.WebSocketClientProtocol) -> None:
        """断线重连后逐条重放 result_queue。"""
        async with self._result_queue_lock:
            conn = get_connection()
            try:
                rows = conn.execute("SELECT id, task_id, payload FROM result_queue ORDER BY id").fetchall()
                for row in rows:
                    result_data = json.loads(row["payload"])
                    msg = {"type": "task_result", "data": result_data}
                    try:
                        await ws.send(json.dumps(msg, ensure_ascii=False))
                        conn.execute("DELETE FROM result_queue WHERE id = ?", (row["id"],))
                        conn.commit()
                        logger.info("Replayed result for task %s", row["task_id"])
                    except Exception:
                        logger.debug("Failed to replay result for task %s", row["task_id"])
                        break
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # ERROR 日志上报
    # ------------------------------------------------------------------
    async def report_error(self, module: str, message: str, stack_trace: str = "") -> None:
        """上报 ERROR 日志（60s 去重 + 脱敏）。"""
        # 去重检查
        dedup_key = f"{module}:{message}"
        now = time.time()
        last_time = self._error_dedup.get(dedup_key, 0)
        if now - last_time < self._error_dedup_ttl:
            return
        self._error_dedup[dedup_key] = now

        # 脱敏
        safe_message = _sanitize(message)
        safe_stack = _sanitize(stack_trace[:500]) if stack_trace else ""

        if not self._ws:
            return

        msg = {
            "type": "error_log",
            "data": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": "ERROR",
                "module": module,
                "message": safe_message,
                "stack_trace": safe_stack,
            },
        }
        try:
            await self._ws.send(json.dumps(msg, ensure_ascii=False))
        except Exception:
            logger.debug("Failed to send error_log")

    # ------------------------------------------------------------------
    # 连接状态通知
    # ------------------------------------------------------------------
    def _notify_connection(self, connected: bool) -> None:
        if self.on_connection_change:
            try:
                self.on_connection_change(connected)
            except Exception:
                logger.debug("Error in connection change callback")

    # ------------------------------------------------------------------
    # 重载配置（重连 WS）
    # ------------------------------------------------------------------
    def reload_config(self, new_config: dict[str, Any]) -> None:
        """热重载配置：更新配置并触发重连。"""
        self._config.update(new_config)
        save_config(self._config)
        # 更新 token/配置后清除上次关闭原因（不改变退出语义，仅清字段）
        self._last_close_reason = None
        # 唤醒凭证失效挂起的主循环。reload_config 由 local_api 的 aiohttp 协程处理器调用，
        # 与 core.run() 同一事件循环（见 service_host._main / runner.run_foreground），
        # 因此 Event.set() 可直接调用，无需 call_soon_threadsafe。
        if self._resume_event is not None:
            self._resume_event.set()
        logger.info("Config reloaded")
        # 关闭当前连接触发重连
        if self._ws:
            asyncio.ensure_future(self._ws.close())
