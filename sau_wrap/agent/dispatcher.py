# -*- coding: utf-8 -*-
"""任务执行核心（实施计划 S4；现状文档 §5.3 流水线、§5.4 结果补发、§4.5 素材重签）。

流水线：收到 ``publish_task`` → 幂等落 ``local_tasks``(queued) → 信号量并发控制
（默认 2，``config.json`` ``max_concurrency`` 可配）→ 账号解析 → 素材下载
（403 → ``file_renew`` → 收到 ``file_renewed`` 换新 URL 重新下载，重签上限 2 次防死循环）
→ ``upstream_adapter`` 调用上游上传器（import 方式，绝不修改上游）→ ``task_result``
回报（先落 ``result_queue`` 再发送，断线留存补发）→ 清理 ``downloads/{task_id}``。

可靠性语义（§3.8 保留项）：
- cookie 错误不自动重试（``_is_cookie_error`` 关键词分类，现状语义）；
- 时钟暂停标志（``scheduling_paused`` / 凭证过期）生效时新任务保持 queued 不执行；
- 重启恢复：``recover_pending()`` 启动时扫 queued/running 重新入队。

可测试性：``downloader`` / ``after_task_hook`` 可注入；上传函数经
``upstream_adapter.register_uploader`` 注入（假实现），真实路径保留。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from sau_wrap import paths
from sau_wrap.agent import accounts as accounts_mod
from sau_wrap.agent import db
from sau_wrap.agent import upstream_adapter

#: 素材重签（403 → file_renew）最大次数（防服务端回退原 URL 造成死循环，§4.5）
MAX_FILE_RENEWS = 2

#: 等待 file_renewed 的超时（秒）
FILE_RENEWED_TIMEOUT = 60.0

#: 时钟暂停期间的任务复查间隔（秒）
PAUSED_POLL_SECONDS = 30.0

#: 下载分块大小（现状 §5.3：512KB）
DOWNLOAD_CHUNK = 512 * 1024

#: cookie 错误关键词（现状 §5.3 _is_cookie_error）
_COOKIE_ERROR_KEYWORDS = (
    "cookie", "login", "auth", "session expired", "not logged in", "未登录",
    "登录", "失效",
)

#: 网络错误关键词（现状 §5.3 _is_network_error）
_NETWORK_ERROR_KEYWORDS = (
    "connection", "timeout", "network", "dns", "socket", "connect", "超时", "网络",
)


class FileRenewNeeded(Exception):
    """素材下载遇 403（OSS 签名过期），需上行 file_renew 重签（§4.5）。"""


def is_cookie_error(exc: BaseException) -> bool:
    """异常分类：cookie/登录态类错误 → failed 且不自动重试（现状语义）。"""
    text = str(exc).lower()
    return any(k in text for k in _COOKIE_ERROR_KEYWORDS)


def is_network_error(exc: BaseException) -> bool:
    """异常分类：网络类错误（服务端可按规则重投任务）。"""
    if isinstance(exc, (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, OSError)):
        return True
    text = str(exc).lower()
    return any(k in text for k in _NETWORK_ERROR_KEYWORDS)


async def default_downloader(url: str, dest: Path) -> None:
    """aiohttp 下载（512KB 分块，§5.3）；403 抛 ``FileRenewNeeded``。"""
    paths.ensure_dir(dest.parent)
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 403:
                raise FileRenewNeeded(f"403 签名过期: {url}")
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.content.iter_chunked(DOWNLOAD_CHUNK):
                    f.write(chunk)


class TaskDispatcher:
    """任务执行调度器（与服务进程同事件循环）。

    参数：
    - ``result_sender``：``async (task_id, status, error, publish_url, remarks) -> None``，
      由 ``WSClient`` 注入（先落 result_queue，在线即发，断线留存补发）；
    - ``file_renew_sender``：``async (task_id) -> bool``，上行 ``file_renew``；
    - ``scheduling_paused``：时钟暂停/凭证过期判定（为 True 时新任务保持 queued）；
    - ``stop_event``：服务停止事件；
    - ``downloader``：可注入下载函数（测试用 mock http），默认 aiohttp 实现；
    - ``after_task_hook``：任务结束（成功或失败）后回调（本步用于触发 account_sync）。
    """

    def __init__(
        self,
        logger: logging.Logger,
        *,
        result_sender,
        file_renew_sender,
        scheduling_paused,
        stop_event: asyncio.Event,
        max_concurrency: int = 2,
        downloader=None,
        after_task_hook=None,
    ) -> None:
        self._logger = logger
        self._result_sender = result_sender
        self._file_renew_sender = file_renew_sender
        self._scheduling_paused = scheduling_paused
        self._stop = stop_event
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._downloader = downloader or default_downloader
        self._after_task_hook = after_task_hook
        self._in_flight: set[str] = set()
        #: task_id → 待 file_renewed 的 Future
        self._renew_waiters: dict[str, asyncio.Future] = {}
        #: 重启恢复幂等标志（首次注册就绪后只执行一次）
        self._recover_done = False

    # ------------------------------------------------------------ 入队

    def submit(self, payload: dict) -> None:
        """接收 publish_task：幂等落库（INSERT OR REPLACE 语义）+ 起执行协程。

        本地终态保护：已 success 的任务重推直接跳过（现实路径：结果积压
        result_queue 未回达时服务端补推）——重复执行会造成真实重复发布；
        failed 不拦（重推是合法的服务端重试语义）。
        """
        task_id = str(payload.get("task_id") or "")
        if not task_id:
            self._logger.warning("publish_task 缺少 task_id，忽略: %.200s", payload)
            return
        existing = db.get_task(task_id)
        if existing is not None and existing[2] == "success":
            self._logger.info(
                "任务本地已 success，跳过重推（防重复发布）: task_id=%s", task_id
            )
            return
        db.upsert_task(
            task_id,
            json.dumps(payload, ensure_ascii=False),
            status="queued",
            run_at=_ms_to_iso(payload.get("scheduled_at")),
        )
        if task_id in self._in_flight:
            self._logger.info("任务已在执行链路中，幂等跳过重复入队: %s", task_id)
            return
        self._in_flight.add(task_id)
        asyncio.create_task(self._worker(task_id, payload))
        self._logger.info("任务已入队: task_id=%s platform=%s content_type=%s",
                          task_id, payload.get("platform_key"), payload.get("content_type"))

    def recover_pending(self) -> int:
        """重启恢复（§5.3）：扫 queued/running 重新入队。返回恢复条数。"""
        rows = db.list_pending_tasks()
        count = 0
        for task_id, payload_json in rows:
            try:
                payload = json.loads(payload_json)
            except ValueError:
                self._logger.error("恢复失败：任务载荷非法 JSON: task_id=%s", task_id)
                db.set_task_status(task_id, "failed")
                continue
            self.submit(payload)
            count += 1
        if count:
            self._logger.info("重启恢复：重新入队 %d 个未完成任务", count)
        return count

    def recover_pending_once(self) -> int:
        """幂等版重启恢复：由 ``WSClient`` 在**首次收到 registered** 后调用。

        原因：恢复入队若早于会话就绪，下载 403 需重签时 ``file_renew`` 无连接
        可发，任务会直接 failed；延迟到注册后入队，重签链路即刻可用。
        """
        if self._recover_done:
            return 0
        self._recover_done = True
        return self.recover_pending()

    # ------------------------------------------------------------ file_renew 协议

    def on_file_renewed(self, data: dict) -> None:
        """收到服务端 ``file_renewed``：唤醒等待中的下载重试。"""
        task_id = str(data.get("task_id") or "")
        fut = self._renew_waiters.get(task_id)
        if fut is not None and not fut.done():
            fut.set_result(data)

    async def _request_renewed_url(self, task_id: str) -> dict | None:
        """上行 ``file_renew`` 并等待 ``file_renewed``（超时返回 None）。"""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._renew_waiters[task_id] = fut
        try:
            sent = await self._file_renew_sender(task_id)
            if not sent:
                self._logger.warning("file_renew 发送失败（WS 离线）: task_id=%s", task_id)
                return None
            return await asyncio.wait_for(fut, timeout=FILE_RENEWED_TIMEOUT)
        except asyncio.TimeoutError:
            self._logger.warning("等待 file_renewed 超时: task_id=%s", task_id)
            return None
        finally:
            self._renew_waiters.pop(task_id, None)

    # ------------------------------------------------------------ 执行流水线

    async def _worker(self, task_id: str, payload: dict) -> None:
        try:
            await self._execute(task_id, payload)
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception:
            self._logger.exception("任务执行协程异常: task_id=%s", task_id)
            await self._finish(task_id, payload, "failed", "执行器内部异常", "")
        finally:
            self._in_flight.discard(task_id)

    async def _execute(self, task_id: str, payload: dict) -> None:
        # 时钟暂停保护（§3.8 #1：偏差 >5 分钟暂停调度；4410 凭证过期同理）
        # 等待可被 stop 打断（服务停止时不阻塞到满 30s）
        while self._scheduling_paused() and not self._stop.is_set():
            db.set_task_status(task_id, "queued")
            self._logger.info("时钟暂停/凭证过期，任务保持 queued 不执行: %s", task_id)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=PAUSED_POLL_SECONDS)
            except asyncio.TimeoutError:
                continue
            return  # stop 置位：任务保持 queued，留待重启恢复
        if self._stop.is_set():
            return

        async with self._semaphore:
            if self._stop.is_set():
                return
            db.bump_task_attempts(task_id)
            db.set_task_status(task_id, "running")
            self._logger.info("任务开始执行: task_id=%s", task_id)

            # 1) 账号解析（§5.3）
            platform_key = str(payload.get("platform_key") or "")
            account_name = payload.get("account_name") or None
            account = accounts_mod.resolve_account(platform_key, account_name)
            if account is None:
                reason = (
                    f"无可用账号：{platform_key} 平台"
                    + (f" 指定账号 {account_name} 不存在或无效" if account_name else " 无有效 cookie")
                    + "（不自动重试；请先登录获取 cookie）"
                )
                self._logger.warning("任务失败（账号）: task_id=%s %s", task_id, reason)
                await self._finish(task_id, payload, "failed", reason, "")
                return

            # 2) 素材下载（含 403 重签，§4.5）
            work_dir = paths.ensure_dir(paths.DOWNLOADS_DIR / task_id)
            renew_count = 0
            try:
                while True:
                    try:
                        video_file, image_files = await self._download(task_id, payload, work_dir)
                        break
                    except FileRenewNeeded:
                        if renew_count >= MAX_FILE_RENEWS:
                            raise RuntimeError(
                                f"素材重签已达上限（{MAX_FILE_RENEWS} 次），仍 403，放弃"
                            )
                        renew_count += 1
                        self._logger.warning(
                            "素材 403，上行 file_renew 请求重签（第 %d/%d 次）: task_id=%s",
                            renew_count, MAX_FILE_RENEWS, task_id,
                        )
                        renewed = await self._request_renewed_url(task_id)
                        if renewed is None:
                            raise RuntimeError("素材重签失败：未收到 file_renewed（离线或超时）")
                        if renewed.get("file_url"):
                            payload["file_url"] = renewed["file_url"]
                        if renewed.get("media_urls"):
                            payload["media_urls"] = renewed["media_urls"]
                        db.update_task_payload(task_id, json.dumps(payload, ensure_ascii=False))
            except Exception as exc:
                await self._finish(task_id, payload, "failed", f"素材下载失败: {exc}", "")
                return

            # 3) 调用上游上传器（适配层）
            try:
                publish_url = await upstream_adapter.execute_upload(
                    platform_key,
                    str(payload.get("content_type") or ""),
                    payload=payload,
                    account_file=str(
                        accounts_mod.find_account_file(
                            platform_key, account["account_name"]
                        ) or ""
                    ),
                    video_file=video_file,
                    image_files=image_files,
                )
            except Exception as exc:
                if is_cookie_error(exc):
                    error = f"cookie 错误（不自动重试）: {exc}"
                elif is_network_error(exc):
                    error = f"网络错误: {exc}"
                else:
                    error = f"上传失败: {exc}"
                self._logger.warning("任务失败: task_id=%s %s", task_id, error)
                await self._finish(task_id, payload, "failed", error, "")
                return

            # 4) 成功回报（manual 语义备注，§5.3）
            remarks = ""
            if payload.get("submit_mode") == "manual" and platform_key != "tencent":
                remarks = "平台不支持草稿，已直接发布"
            await self._finish(task_id, payload, "success", "", publish_url, remarks)

    async def _download(self, task_id: str, payload: dict, work_dir: Path):
        """video 用 file_url 单文件；note 用 media_urls 多图（§5.3）。"""
        content_type = str(payload.get("content_type") or "")
        if content_type == "note":
            media_urls = [u for u in (payload.get("media_urls") or []) if u]
            if not media_urls:
                raise RuntimeError("note 任务缺少 media_urls")
            image_files: list[Path] = []
            for i, url in enumerate(media_urls):
                dest = work_dir / f"image_{i}{_url_ext(url, '.jpg')}"
                await self._downloader(url, dest)
                image_files.append(dest)
            self._logger.info("素材下载完成（%d 张图）: task_id=%s", len(image_files), task_id)
            return None, image_files
        file_url = str(payload.get("file_url") or "")
        if not file_url:
            raise RuntimeError("video 任务缺少 file_url")
        video_file = work_dir / f"video{_url_ext(file_url, '.mp4')}"
        await self._downloader(file_url, video_file)
        self._logger.info("素材下载完成（视频）: task_id=%s", task_id)
        return video_file, []

    # ------------------------------------------------------------ 收尾

    async def _finish(
        self,
        task_id: str,
        payload: dict,
        status: str,
        error: str = "",
        publish_url: str = "",
        remarks: str = "",
    ) -> None:
        db.set_task_status(task_id, status)
        await self._result_sender(task_id, status, error, publish_url, remarks)
        # 完成后清理 downloads/{task_id}（§3.6 按任务隔离的工作目录）
        shutil.rmtree(paths.DOWNLOADS_DIR / task_id, ignore_errors=True)
        self._logger.info("任务结束: task_id=%s status=%s", task_id, status)
        if self._after_task_hook is not None:
            try:
                await self._after_task_hook()
            except Exception:
                self._logger.warning(
                    "任务结束回调异常（不影响任务结果）: task_id=%s", task_id,
                    exc_info=True,
                )


# ---------------------------------------------------------------- 工具


def _url_ext(url: str, default: str) -> str:
    """从 URL 路径推断扩展名（白名单防注入），否则默认。"""
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".mp4", ".mov", ".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return suffix
    return default


def _ms_to_iso(ms) -> str | None:
    if not ms:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
