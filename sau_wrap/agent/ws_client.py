# -*- coding: utf-8 -*-
"""Agent WS 主循环（实施计划 S2；现状文档 §3 协议、§5.1-§5.4 可靠性语义）。

职责（本步范围）：
- 连接 ``{server_url}?agentId=&machine=``，Header ``Authorization: Bearer <token>``；
- 首包 ``register``，处理 ``registered``；
- 心跳 30s（含 ``active_tasks`` 与 ``clock_offset_seconds``），``heartbeat_ack.server_time``
  做滑动平均（10 样本）估算时钟偏差，偏差 >5 分钟置调度暂停标志（本步仅标志位）；
- 重连退避 2s→300s（指数翻倍，收到 registered 复位；含服务端主动 1000 关闭也重连）；
- 关闭码 4401/4403/4409/4410 → 挂起零重连，等待 ``resume_event``（配置热重载）唤醒；
- ``publish_task``：记日志 + 落 ``local_tasks``（真实执行后续步骤）；
- ``task_result``：先落 ``result_queue`` 再发送，成功后删除；重连后补发；
- 优雅停止：响应 ``stop_event``，退出前关闭连接。

事件循环：由宿主以 ``WindowsSelectorEventLoopPolicy`` 驱动（§5.1：规避 Session 0
Proactor 管道竞态）。本模块不自行建事件循环，便于测试注入。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from sau_wrap.agent import accounts as accounts_mod
from sau_wrap.agent import config as agent_config
from sau_wrap.agent import db
from sau_wrap.agent.core import ClockTracker
from sau_wrap.agent.machine import get_machine_code
from sau_wrap.version import APP_VERSION

#: 重连退避（现状文档 §5.2）：初始 2s，指数翻倍，上限 300s，
#: 收到 registered（会话就绪）后才复位，防"连上即被踢"高频重连
BACKOFF_INITIAL = 2
BACKOFF_MAX = 300

#: 凭证类关闭码（§3.5）：一律挂起零重连，等待凭证更新唤醒
SUSPEND_CLOSE_CODES = frozenset({4401, 4403, 4409, 4410})

#: 未绑定时配置轮询间隔（秒）：等待 bind / 热重载唤醒期间周期性复查
UNBOUND_POLL_SECONDS = 30

#: result_queue 补发批次间隔（秒）：逐批清空，避免对服务端瞬间冲击与死循环风险间取平衡
FLUSH_BATCH_INTERVAL = 0.5


def _msg(msg_type: str, data: dict) -> str:
    """标准封套（§3.2）：``{"type": ..., "data": {...}}``。"""
    return json.dumps({"type": msg_type, "data": data}, ensure_ascii=False)


def _ms_to_iso(ms: int | float | None) -> str | None:
    """毫秒时间戳 → ISO 字符串（publish_task.scheduled_at → local_tasks.run_at）。"""
    if not ms:
        return None
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class WSClient:
    """opcgeo Agent WS 客户端主循环。

    参数：
    - ``stop_event``：asyncio.Event，宿主（服务停止 / 前台 Ctrl+C）置位后优雅退出；
    - ``resume_event``：asyncio.Event，配置热重载（S3 的 POST /config）置位唤醒挂起态。
    """

    def __init__(
        self,
        logger: logging.Logger,
        stop_event: asyncio.Event,
        resume_event: asyncio.Event,
        dispatcher=None,
    ) -> None:
        self._logger = logger
        self._stop = stop_event
        self._resume = resume_event
        self._dispatcher = dispatcher
        self._updater = None  # S7：升级状态机（host 接线）
        self._clock = ClockTracker()
        self._registered = False
        self._token_expired = False
        #: 重连退避（实例态）：收到 registered（会话真正就绪）后复位，
        #: 避免"连上即被踢"时退避永不增长（现状文档 §5.2）
        self._backoff = BACKOFF_INITIAL
        #: 最近一次 registered 下发的有效期（毫秒或 None）
        self.expire_at: int | None = None
        # ---- 可观测状态（供 5409 本地 API /status 读取，§3.7 契约）----
        #: WS 会话是否存活（连接建立→会话结束）
        self.ws_connected = False
        #: 是否处于凭证类挂起态（零重连等待唤醒）
        self.suspended = False
        #: 最近一次断开原因（关闭码 + 语义，供排障与 /status）
        self.last_close_reason: str | None = None
        #: 当前连接引用（热重载时主动关闭触发重连）
        self._current_ws = None

    # ------------------------------------------------------------ 主循环

    def attach_dispatcher(self, dispatcher) -> None:
        """挂载任务执行器（S4）；未挂载时 publish_task 仅落库（S2 骨架语义）。"""
        self._dispatcher = dispatcher

    def attach_updater(self, updater) -> None:
        """挂载升级状态机（S7）；未挂载时 upgrade_notice 仅记日志（向后兼容）。"""
        self._updater = updater

    async def run(self) -> None:
        """外层主循环：退避重连 + 凭证类关闭码挂起，直至 stop。"""
        db.db_init()
        self._logger.info("ws_client 主循环启动: version=%s", APP_VERSION)
        while not self._stop.is_set():
            cfg = agent_config.load_config()
            token = agent_config.load_token()
            if cfg is None or token is None:
                self._logger.warning(
                    "未绑定或凭证缺失（config.json/credential.bin），等待 bind / 配置热重载唤醒"
                )
                if not await self._wait_resume_or_poll(UNBOUND_POLL_SECONDS):
                    continue  # 轮询超时：复查配置
                if self._stop.is_set():
                    break
                continue  # 被唤醒：立即复查配置

            try:
                machine = await asyncio.to_thread(get_machine_code)  # 终审修复12：首算不阻塞事件循环（结果缓存）
            except RuntimeError as exc:
                self._logger.error("机器码生成失败，%s 秒后重试: %s", self._backoff, exc)
                if await self._sleep_interruptible(self._backoff):
                    break
                self._backoff = min(self._backoff * 2, BACKOFF_MAX)
                continue

            url = f"{cfg.server_url}?agentId={cfg.agent_id}&machine={machine}"
            if str(cfg.server_url).lower().startswith("ws://"):
                # 终审修复14：明文连接强警告（生产建议 wss；doctor 亦提示）
                self._logger.warning(
                    "使用 ws:// 明文连接（强警告：生产环境建议使用 wss:// "
                    "加密传输）: %s", cfg.server_url,
                )
            self._logger.info("连接 WS: %s?agentId=%s&machine=%s…",
                              cfg.server_url, cfg.agent_id, machine[:8])
            try:
                async with connect(
                    url,
                    additional_headers={"Authorization": f"Bearer {token}"},
                    ping_interval=None,  # 现状文档 §5.1：应用层心跳，关闭库级 ping
                    open_timeout=15,
                ) as ws:
                    self._logger.info("WS 连接已建立")
                    normal_exit = await self._session(ws, cfg, machine)
                    if normal_exit:  # 优雅停止（仅停止信号触发）
                        break
            except ConnectionClosed as exc:
                # 统一分类（ConnectionClosedOK 亦为其子类）：关闭握手完成与否决定
                # OK/非 OK，但凭证类关闭码判定不受影响，必须在退避之前。
                code = exc.rcvd.code if exc.rcvd is not None else None
                reason = getattr(exc.rcvd, "reason", "") or ""
                if code in SUSPEND_CLOSE_CODES:
                    self.last_close_reason = f"凭证类关闭码 {code}（{reason or '-'}），已挂起"
                    self._logger.warning(
                        "服务端以凭证类关闭码 %s 关闭连接（%s），进入挂起零重连，"
                        "等待凭证更新（配置热重载）唤醒",
                        code, reason or "-",
                    )
                    if code == 4410:
                        self._token_expired = True
                    await self._wait_for_resume()
                    if self._stop.is_set():
                        break
                    continue
                if self._stop.is_set():
                    break  # 优雅停止（仅停止信号触发才退出主循环）
                # 其余码（含服务端主动 1000，如发版重启）：退避重连，不退出进程；
                # 不属凭证类，不挂起。
                self.last_close_reason = f"关闭码 {code}（{reason or '-'}）"
                self._logger.warning(
                    "WS 连接被关闭（code=%s），%s 秒后重连", code, self._backoff
                )
            except OSError as exc:
                self.last_close_reason = f"连接失败: {exc}"
                self._logger.warning("WS 连接失败: %s，%s 秒后重连", exc, self._backoff)
            except Exception:
                self.last_close_reason = "会话异常（详见日志）"
                self._logger.exception("WS 会话异常，%s 秒后重连", self._backoff)

            if await self._sleep_interruptible(self._backoff):
                break
            self._backoff = min(self._backoff * 2, BACKOFF_MAX)

        self._logger.info("ws_client 主循环退出（停止信号）")

    # ------------------------------------------------------------ 会话

    async def _session(self, ws, cfg, machine: str) -> bool:
        """单次连接会话：注册 → 补发队列 → 心跳 + 接收分发。返回 True=优雅停止。"""
        self._registered = False
        self._current_ws = ws
        self.ws_connected = True
        stop_task = None
        hb_task = None
        try:
            try:
                await ws.send(_msg("register", {
                    "agent_id": cfg.agent_id,
                    "machine_code": machine,
                    "version": APP_VERSION,
                    "platforms": list(accounts_mod.PLATFORM_KEYS),  # 平台注册表（S4）
                    "accounts": accounts_mod.scan_accounts(),       # 账号快照（S4）
                }))
            except ConnectionClosed:
                # 注册阶段连接即被服务端关闭（如握手后立即 4401/4409）：
                # 不吞异常，交主循环按关闭码分类处理（挂起 / 退避）。
                # 注：外层 finally 保证此时 ws_connected/_current_ws 必复位，
                # 不会出现挂起态快照 ws_connected=True 与 suspended=True 矛盾。
                self._logger.warning("register 发送失败：连接已被服务端关闭")
                raise
            # 停止监视：stop 置位时主动关闭连接，令 recv 抛出 ConnectionClosedOK(1000)
            stop_task = asyncio.create_task(self._stop_and_close(ws))
            hb_task = asyncio.create_task(self._heartbeat_loop(ws, cfg))
            while True:
                raw = await ws.recv()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    self._logger.warning("收到非 JSON 帧，忽略: %.200s", raw)
                    continue
                await self._dispatch(ws, msg)
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd is not None else None
            if self._stop.is_set():
                self._logger.info("WS 连接已关闭（优雅停止，code=%s）", code)
                return True
            # 非停止触发的关闭（含服务端主动 1000）一律交主循环分类处理，
            # 不得返回 True（否则主循环退出，服务进程永久停止）。
            raise
        finally:
            # 包裹整个会话体：任何路径（含 register 阶段抛异常）都必须复位状态，
            # 否则 trigger_reload 会误操作死连接、/status 快照自相矛盾。
            self.ws_connected = False
            self._current_ws = None
            if hb_task is not None:
                hb_task.cancel()
            if stop_task is not None:
                stop_task.cancel()

    async def _stop_and_close(self, ws) -> None:
        """等待 stop 置位后主动关闭连接（优雅停止路径）。"""
        await self._stop.wait()
        self._logger.info("收到停止信号，关闭 WS 连接")
        try:
            await ws.close(1000, "agent stopping")
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------ 心跳

    async def _heartbeat_loop(self, ws, cfg) -> None:
        """每 ``heartbeat_interval`` 秒发送心跳（§3.3）。日志级别 INFO（落盘可见）。"""
        interval = max(1, cfg.heartbeat_interval)
        while True:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return  # stop 置位
            except asyncio.TimeoutError:
                pass
            data = {
                "agent_id": cfg.agent_id,
                "active_tasks": db.count_active_tasks(),
                "accounts": accounts_mod.scan_accounts(),  # 账号快照（S4）
                "clock_offset_seconds": round(self._clock.offset_seconds, 3),
            }
            if not await self._send(ws, "heartbeat", data):
                return
            self._logger.info(
                "心跳已发送: active_tasks=%d clock_offset=%.3fs paused=%s",
                data["active_tasks"], data["clock_offset_seconds"],
                self._clock.scheduling_paused,
            )

    # ------------------------------------------------------------ 消息分发

    async def _dispatch(self, ws, msg: dict) -> None:
        msg_type = msg.get("type")
        data = msg.get("data") or {}
        if msg_type == "registered":
            self._registered = True
            self._backoff = BACKOFF_INITIAL  # 会话真正就绪，退避复位（§5.2）
            self.expire_at = data.get("expire_at")
            self._logger.info(
                "收到 registered: agent_id=%s machine_bound=%s expire_at=%s",
                data.get("agent_id"), data.get("machine_bound"), self.expire_at,
            )
            await self._flush_result_queue(ws)
            if self._dispatcher is not None:
                # 重启恢复延迟到会话就绪（幂等，仅首次）：保证下载 403 需重签时
                # file_renew 有连接可发，避免恢复任务因离线发送失败直接 failed。
                self._dispatcher.recover_pending_once()
        elif msg_type == "heartbeat_ack":
            if data.get("expire_at") is not None:  # 终审修复11：一行级消费续期
                self.expire_at = data["expire_at"]
            server_time = data.get("server_time")
            if server_time is not None:
                avg = self._clock.update(server_time)
                self._logger.info(
                    "heartbeat_ack: server_time=%s 平均偏差=%.3fs（%d 样本）调度暂停=%s",
                    server_time, avg, self._clock.sample_count,
                    self._clock.scheduling_paused,
                )
            if data.get("expired_warning"):
                self._logger.warning("到期预警: %s", data["expired_warning"])
        elif msg_type == "publish_task":
            task_id = str(data.get("task_id") or "")
            self._logger.info(
                "收到 publish_task: task_id=%s platform=%s content_type=%s",
                task_id, data.get("platform_key"), data.get("content_type"),
            )
            if task_id:
                if self._dispatcher is not None:
                    # S4：交执行器（幂等落库 + 并发执行 + 结果回报）
                    self._dispatcher.submit(data)
                else:
                    # 未挂载执行器：仅落库骨架（S2 语义，向后兼容）
                    db.upsert_task(
                        task_id,
                        json.dumps(data, ensure_ascii=False),
                        run_at=_ms_to_iso(data.get("scheduled_at")),
                    )
        elif msg_type == "upgrade_notice":
            # S7：交升级状态机（校验三字段/严格版本大于/域名白名单；不合法拒绝）
            if self._updater is not None:
                accepted = self._updater.handle_notice(data)
                self._logger.info(
                    "收到 upgrade_notice: version=%s → %s",
                    data.get("version"),
                    "受理（状态机推进）" if accepted else "拒绝/忽略（详见日志）",
                )
            else:
                self._logger.info(
                    "收到 upgrade_notice: version=%s（未挂载 updater，仅记录）",
                    data.get("version"),
                )
        elif msg_type == "token_expired":
            self._token_expired = True
            self._logger.warning("收到 token_expired: %s（服务端将以 4410 关闭）", data)
        elif msg_type == "bind_rejected":
            self._logger.warning("收到 bind_rejected: reason=%s", data.get("reason"))
        elif msg_type == "file_renewed":
            self._logger.info(
                "收到 file_renewed: task_id=%s（素材重签新 URL，§4.5）",
                data.get("task_id"),
            )
            if self._dispatcher is not None:
                self._dispatcher.on_file_renewed(data)
        else:
            self._logger.info("收到未处理消息类型: %s", msg_type)

    # ------------------------------------------------------------ 发送

    async def _send(self, ws, msg_type: str, data: dict) -> bool:
        if ws is None:
            return False
        try:
            await ws.send(_msg(msg_type, data))
            return True
        except ConnectionClosed:
            self._logger.warning("发送 %s 失败：连接已关闭", msg_type)
            return False

    async def send_task_result(
        self,
        ws,
        task_id: str,
        status: str,
        error: str = "",
        publish_url: str = "",
        remarks: str = "",
    ) -> bool:
        """task_result 上报（§5.4）：先落 result_queue，发送成功后删除队列项。
        ``ws`` 为 None（离线）时仅落库，留待重连补发。"""
        data = {
            "task_id": task_id,
            "status": status,
            "error": error,
            "publish_url": publish_url,
            "remarks": remarks,
        }
        item_id = db.enqueue_result(task_id, json.dumps(data, ensure_ascii=False))
        self._logger.info("task_result 已落 result_queue: id=%d task_id=%s", item_id, task_id)
        if await self._send(ws, "task_result", data):
            db.delete_result(item_id)
            self._logger.info("task_result 发送成功，队列项已删除: id=%d", item_id)
            return True
        self._logger.warning("task_result 发送失败，保留队列项待补发: id=%d", item_id)
        return False

    async def _flush_result_queue(self, ws) -> None:
        """连接就绪后补发 result_queue（断线期间的结果回报，§5.4）。

        逐批循环清空（每批 ``peek_results`` 默认 50 项），批次间隔 ``FLUSH_BATCH_INTERVAL``；
        发送失败（连接关闭）/停止信号即退出，剩余项留待下次重连——不会死循环：
        每轮要么删除若干队列项，要么直接 return。
        """
        total_sent = 0
        while pending := db.peek_results():
            self._logger.info("补发 result_queue: 本批 %d 项", len(pending))
            for item_id, task_id, payload in pending:
                if self._stop.is_set():
                    return
                try:
                    data = json.loads(payload)
                except ValueError:
                    self._logger.error("队列项 payload 非法 JSON，丢弃: id=%d", item_id)
                    db.delete_result(item_id)
                    continue
                if not await self._send(ws, "task_result", data):
                    self._logger.warning("补发中断（连接关闭），剩余项留待下次重连")
                    return
                db.delete_result(item_id)
                total_sent += 1
                self._logger.info("补发成功，队列项已删除: id=%d task_id=%s", item_id, task_id)
            if db.peek_results():  # 还有下一批：间隔后再取，避免瞬间冲击
                await asyncio.sleep(FLUSH_BATCH_INTERVAL)
        if total_sent:
            self._logger.info("result_queue 补发完成，共 %d 项，队列已清空", total_sent)

    # ------------------------------------------------------------ 上行辅助（S4）

    async def submit_task_result(
        self,
        task_id: str,
        status: str,
        error: str = "",
        publish_url: str = "",
        remarks: str = "",
    ) -> bool:
        """任务结果回报统一入口（供 dispatcher 注入）：先落 result_queue，
        在线即发、离线留存，重连后由补发循环清完（§5.4）。"""
        return await self.send_task_result(
            self._current_ws, task_id, status, error, publish_url, remarks
        )

    async def send_file_renew(self, task_id: str) -> bool:
        """上行 ``file_renew{task_id}``（素材 403 重签，§4.5）。"""
        return await self._send(self._current_ws, "file_renew", {"task_id": task_id})

    async def send_account_sync(self) -> bool:
        """上行 ``account_sync{accounts}``（§3.3）：账号快照变更同步。"""
        if self._current_ws is None:
            return False
        snapshot = accounts_mod.scan_accounts()
        return await self._send(self._current_ws, "account_sync", {"accounts": snapshot})

    # ------------------------------------------------------------ 热重载（S3 本地 API 触发）

    def trigger_reload(self) -> str:
        """配置热重载入口（现状文档 §5.2）：唤醒挂起态；若当前有连接则主动关闭，
        令主循环重读 config/凭证后重连。返回描述（供 API 响应/审计）。"""
        self._resume.set()
        ws = self._current_ws
        if ws is not None:
            self.last_close_reason = "本地热重载（配置/凭证更新）"
            asyncio.create_task(self._close_for_reload(ws))
            return "已唤醒：当前连接将关闭并以新配置重连"
        return "已唤醒：挂起/等待态将立即复查配置"

    async def _close_for_reload(self, ws) -> None:
        try:
            await ws.close(1000, "reload")
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------ 可观测快照（§3.7 契约）

    def is_scheduling_paused(self) -> bool:
        """轻量谓词（供 dispatcher 的 scheduling_paused 注入）：时钟偏差暂停或
        凭证过期时为 True。不读 DB/磁盘，可高频调用。"""
        return self._clock.scheduling_paused or self._token_expired

    def status_snapshot(self) -> dict:
        """只读状态快照（供 5409 本地 API ``GET /status``），
        收敛对内部私有属性的直接读取。字段结构按 §3.7 契约固定。"""
        return {
            "ws_connected": self.ws_connected,
            "suspended": self.suspended,
            "version": APP_VERSION,
            "active_tasks": db.count_active_tasks(),
            "accounts": accounts_mod.scan_accounts(),  # 账号快照（S4：双目录兼容扫描）
            "clock_offset_seconds": round(self._clock.offset_seconds, 3),
            "scheduling_paused": self._clock.scheduling_paused,
            "token_expire_at": self.expire_at,
            "token_expired": self._token_expired,
            "last_close_reason": self.last_close_reason,
        }

    # ------------------------------------------------------------ 等待原语

    async def _sleep_interruptible(self, seconds: float) -> bool:
        """退避等待；stop 置位提前返回（应退出）；resume 置位（配置热重载）
        同样打断退避立即重连（§5.2：断线退避期间修正配置后不必等满最长 300s）。"""
        resume_task = asyncio.create_task(self._resume.wait())
        stop_task = asyncio.create_task(self._stop.wait())
        try:
            done, pending = await asyncio.wait(
                {resume_task, stop_task},
                timeout=seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:  # pragma: no cover
            resume_task.cancel()
            stop_task.cancel()
            raise
        for task in pending:
            task.cancel()
        if stop_task in done:
            return True
        if resume_task in done:
            self._resume.clear()
            self._logger.info("配置热重载打断退避等待，立即重连")
        return False

    async def _wait_for_resume(self) -> None:
        """挂起零重连：等待 resume（配置热重载）或 stop（§3.5 _wait_for_resume 语义）。"""
        # 入口先清除可能残留的 resume 标志（防虚唤醒：置位早于进入等待的旧信号）
        self._resume.clear()
        self.suspended = True
        self._logger.info("进入挂起状态，等待配置热重载唤醒或停止信号")
        try:
            while not self._stop.is_set():
                resume_task = asyncio.create_task(self._resume.wait())
                stop_task = asyncio.create_task(self._stop.wait())
                done, pending = await asyncio.wait(
                    {resume_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                if resume_task in done:
                    self._resume.clear()
                    self._token_expired = False
                    self._logger.info("被配置热重载唤醒，退出挂起状态，重新连接")
                    return
        finally:
            self.suspended = False

    async def _wait_resume_or_poll(self, poll_seconds: float) -> bool:
        """未绑定等待：被唤醒返回 True；超过 poll_seconds 返回 False（复查配置）。"""
        try:
            resume_task = asyncio.create_task(self._resume.wait())
            stop_task = asyncio.create_task(self._stop.wait())
            done, pending = await asyncio.wait(
                {resume_task, stop_task},
                timeout=poll_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if resume_task in done:
                self._resume.clear()
                return True
            return False
        except asyncio.CancelledError:  # pragma: no cover
            raise

