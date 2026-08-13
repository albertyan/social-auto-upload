"""
sau_agent_pkg.dispatcher
~~~~~~~~~~~~~~~~~~~~~~~~
任务调度器：接收 publish_task → 下载素材 → 构建 UploadRequest → 上传 → 回报结果。

职责：
- asyncio.Semaphore 控制并发
- 素材异步下载（aiohttp）
- 经 upstream_adapter 调用上游 upload 函数
- 进度/结果通过回调函数上报
- 异常分类：cookie 失效 → failed；网络错误 → file_renew 或重试
- 任务落 local_tasks 表，重启后 recover_pending 恢复
- 任务完成后清理下载文件
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import aiohttp

from sau_agent_pkg import accounts
from sau_agent_pkg.config import SAU_HOME
from sau_agent_pkg.db_init import get_connection
from sau_agent_pkg.upstream_adapter import PLATFORMS, get_request_class, get_upload_fn

logger = logging.getLogger(__name__)

_DOWNLOADS_DIR = SAU_HOME / "downloads"


def _now_ms() -> int:
    return int(time.time() * 1000)


class Dispatcher:
    """publish_task 调度器。"""

    def __init__(
        self,
        max_concurrency: int = 1,
        progress_callback: Optional[Callable] = None,
        result_callback: Optional[Callable] = None,
        file_renew_callback: Optional[Callable] = None,
    ) -> None:
        """
        Args:
            max_concurrency: 最大并发上传任务数
            progress_callback: 进度上报回调 async (task_id, stage, percent, message) -> None
            result_callback: 结果上报回调 async (task_id, status, error, publish_url) -> None
            file_renew_callback: 素材重签请求回调 async (task_id) -> None
        """
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._progress_cb = progress_callback
        self._result_cb = result_callback
        self._file_renew_cb = file_renew_callback
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._paused = False
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # 初始未暂停

    @property
    def active_count(self) -> int:
        return len(self._active_tasks)

    # ------------------------------------------------------------------
    # 暂停/恢复（时钟偏差告警时使用）
    # ------------------------------------------------------------------
    def pause(self) -> None:
        """暂停调度：新任务只入队不执行，已执行的任务等待恢复。"""
        self._paused = True
        self._pause_event.clear()
        logger.info("Dispatcher paused (clock drift)")

    def resume(self) -> None:
        """恢复调度：唤醒所有等待的任务，扫描 queued 任务依次执行。"""
        self._paused = False
        self._pause_event.set()
        logger.info("Dispatcher resumed")
        # 触发恢复 queued 任务
        asyncio.ensure_future(self._recover_queued())

    # ------------------------------------------------------------------
    # 提交任务
    # ------------------------------------------------------------------
    def submit(self, task: dict) -> None:
        """
        接收 publish_task，落 local_tasks 表（status=queued），创建 asyncio.Task 执行。
        若同一 task_id 已有活跃任务（如 file_renewed 场景），先取消旧任务。
        """
        task_id = task["task_id"]
        platform_key = task.get("platform_key", "")
        account_name = task.get("account_name", "")
        # info 记录 task_id+平台+账号：服务端下发任务后本地能对应上是哪条账号跑哪条任务
        logger.info("submit: task_id=%s platform=%s account=%s", task_id, platform_key, account_name)
        # 取消已存在的同名任务（e.g. file_renewed 替换正在运行的任务）
        existing = self._active_tasks.pop(task_id, None)
        if existing and not existing.done():
            existing.cancel()
            logger.info("Cancelled existing task %s before re-submit", task_id)
        # 落库
        self._save_task(task, status="queued")
        # 创建执行协程
        aio_task = asyncio.create_task(self._run_with_semaphore(task))
        self._active_tasks[task_id] = aio_task
        aio_task.add_done_callback(lambda t: self._active_tasks.pop(task_id, None))

    # ------------------------------------------------------------------
    # 核心执行流程
    # ------------------------------------------------------------------
    async def _run_with_semaphore(self, task: dict) -> None:
        """等待暂停恢复 + 信号量控制并发后执行任务。"""
        # 等待暂停恢复
        await self._pause_event.wait()
        async with self._semaphore:
            await self._execute(task)

    async def _execute(self, task: dict) -> None:
        """
        下载素材 → 构建 UploadRequest → 调用 upstream_adapter upload → 回报结果。
        """
        task_id = task["task_id"]
        platform_key = task["platform_key"]
        content_type = task.get("content_type", "video")
        start_ms = _now_ms()
        # 开始 info：任务真正进入执行阶段，便于和 submit 入队日志区分
        logger.info("_execute: start task_id=%s platform=%s content_type=%s", task_id, platform_key, content_type)

        # 更新状态为 running
        self._update_task_status(task_id, "running")
        await self._report_progress(task_id, "downloading", 0, "Starting download")

        try:
            phase_start_ms = _now_ms()
            # 1. 解析账号
            account_name = task.get("account_name") or await accounts.first_valid(platform_key)
            if not account_name:
                # 账号解析失败 info：不是 cookie 过期，而是连可用账号都找不到（平台无账号导入或全失效）
                logger.info(
                    "_execute: account resolve failed task_id=%s platform=%s elapsed=%dms",
                    task_id, platform_key, _now_ms() - phase_start_ms,
                )
                await self._report_result(task_id, "failed", "cookie missing/expired", None)
                self._update_task_status(task_id, "failed")
                return
            # 阶段结束 info：账号解析成功，带耗时
            logger.info(
                "_execute: stage [account_resolve] done task_id=%s account=%s elapsed=%dms",
                task_id, account_name, _now_ms() - phase_start_ms,
            )

            # 2. 检查 cookie 有效性
            phase_start_ms = _now_ms()
            caps = PLATFORMS.get(platform_key)
            if not caps:
                # 未知平台 info：平台名在注册表中找不到，一般是服务端下发了新平台但本地 agent 版本旧
                logger.warning(
                    "_execute: unknown platform task_id=%s platform=%s elapsed=%dms",
                    task_id, platform_key, _now_ms() - phase_start_ms,
                )
                await self._report_result(task_id, "failed", f"Unknown platform: {platform_key}", None)
                self._update_task_status(task_id, "failed")
                return

            if not await accounts.check_validity(platform_key, account_name):
                # cookie 无效 warning：账号存在但登录态失效，提示用户去重新扫码/导入 cookie
                logger.warning(
                    "_execute: cookie invalid task_id=%s platform=%s account=%s elapsed=%dms",
                    task_id, platform_key, account_name, _now_ms() - phase_start_ms,
                )
                await self._report_result(task_id, "failed", "cookie missing/expired", None)
                self._update_task_status(task_id, "failed")
                return
            # 阶段结束 info：cookie 校验通过，带耗时
            logger.info(
                "_execute: stage [cookie_check] done task_id=%s account=%s elapsed=%dms",
                task_id, account_name, _now_ms() - phase_start_ms,
            )

            # 3. 下载素材
            phase_start_ms = _now_ms()
            dest_dir = _DOWNLOADS_DIR / task_id
            dest_dir.mkdir(parents=True, exist_ok=True)

            try:
                files = await self._download_materials(task, dest_dir)
            except FileRenewNeeded:
                # file_renew 请求 info 记 task_id：签名 URL 过期需要服务端重签，
                # 打 info 让运维知道任务不是失败，而是在等重签后重跑
                logger.info(
                    "_execute: file_renew requested task_id=%s elapsed=%dms",
                    task_id, _now_ms() - phase_start_ms,
                )
                # 签名 URL 过期，请求重签
                if self._file_renew_cb:
                    await self._file_renew_cb(task_id)
                await self._report_progress(task_id, "downloading", 0, "Waiting for file renew")
                return
            except Exception as e:
                # 下载失败 warning：网络/CDN/磁盘写问题，带 task_id 定位具体素材
                logger.warning(
                    "_execute: download failed task_id=%s error=%s: %s elapsed=%dms",
                    task_id, type(e).__name__, e, _now_ms() - phase_start_ms,
                )
                raise
            # 阶段结束 info：下载成功，带文件数量和耗时
            logger.info(
                "_execute: stage [download] done task_id=%s files=%d elapsed=%dms",
                task_id, len(files), _now_ms() - phase_start_ms,
            )

            await self._report_progress(task_id, "uploading", 50, "Upload starting")

            # 4. 构建 UploadRequest
            phase_start_ms = _now_ms()
            req_cls = get_request_class(platform_key, content_type)
            upload_fn = get_upload_fn(platform_key, content_type)
            if not req_cls or not upload_fn:
                # 不支持 contentType info：该平台不支持视频/图文这种类型，一般是服务端配置错
                logger.info(
                    "_execute: unsupported content_type task_id=%s platform=%s content_type=%s elapsed=%dms",
                    task_id, platform_key, content_type, _now_ms() - phase_start_ms,
                )
                await self._report_result(
                    task_id, "failed",
                    f"Platform {platform_key} does not support {content_type}",
                    None,
                )
                self._update_task_status(task_id, "failed")
                return
            # 阶段结束 info：Request 构建完成
            logger.info(
                "_execute: stage [build_request] done task_id=%s elapsed=%dms",
                task_id, _now_ms() - phase_start_ms,
            )

            req = self._build_request(req_cls, content_type, account_name, task, files)

            # 5. 执行上传
            phase_start_ms = _now_ms()
            await self._report_progress(task_id, "publishing", 70, "Uploading to platform")
            await upload_fn(req)
            # 阶段结束 info：上传调用返回（成功分支）
            logger.info(
                "_execute: stage [upload_fn] done task_id=%s elapsed=%dms",
                task_id, _now_ms() - phase_start_ms,
            )

            # 6. 成功
            await self._report_progress(task_id, "publishing", 100, "Upload complete")
            await self._report_result(task_id, "success", None, None)
            self._update_task_status(task_id, "success")
            # 上传成功 info 带整体耗时：统计端到端 RT，便于容量评估
            logger.info(
                "Task %s completed successfully, total elapsed=%dms",
                task_id, _now_ms() - start_ms,
            )

        except Exception as e:
            error_msg = str(e)
            elapsed_ms = _now_ms() - start_ms
            # 失败分支 warning：统一兜底所有未被分类捕获的异常
            logger.warning(
                "_execute: task failed task_id=%s error=%s: %s total_elapsed=%dms",
                task_id, type(e).__name__, error_msg, elapsed_ms,
            )
            logger.exception("Task %s failed: %s", task_id, error_msg)
            # 异常分类
            if self._is_cookie_error(e):
                await self._report_result(task_id, "failed", "cookie missing/expired", None)
            elif self._is_network_error(e):
                # 网络错误：尝试请求 file_renew
                if self._file_renew_cb:
                    await self._file_renew_cb(task_id)
                await self._report_result(task_id, "failed", f"Network error: {error_msg}", None)
            else:
                await self._report_result(task_id, "failed", error_msg, None)
            self._update_task_status(task_id, "failed")
        finally:
            # 清理下载文件
            self._cleanup_downloads(task_id)

    # ------------------------------------------------------------------
    # 素材下载
    # ------------------------------------------------------------------
    async def _download_materials(self, task: dict, dest_dir: Path) -> list[Path]:
        """
        下载素材到 dest_dir。
        video → file_url；note → media_urls[]。
        """
        content_type = task.get("content_type", "video")
        files: list[Path] = []

        async with aiohttp.ClientSession() as session:
            if content_type == "video":
                file_url = task.get("file_url")
                if not file_url:
                    raise ValueError("Missing file_url for video task")
                path = await self._download_file(session, file_url, dest_dir, "video.mp4")
                files.append(path)
            elif content_type == "note":
                media_urls = task.get("media_urls", [])
                if not media_urls:
                    raise ValueError("Missing media_urls for note task")
                for i, url in enumerate(media_urls):
                    ext = _guess_extension(url, "jpg")
                    path = await self._download_file(session, url, dest_dir, f"media_{i:03d}.{ext}")
                    files.append(path)
            else:
                raise ValueError(f"Unknown content_type: {content_type}")

        return files

    async def _download_file(
        self, session: aiohttp.ClientSession, url: str, dest_dir: Path, filename: str
    ) -> Path:
        """下载单个文件，返回本地路径。"""
        url_prefix = url[:80] if len(url) > 80 else url
        # 开始 debug：排查下载卡住时能看到是哪条 URL 慢
        logger.debug("_download_file: start url_prefix=%s filename=%s", url_prefix, filename)
        dest = dest_dir / filename
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                if resp.status == 403:
                    # 403 warning 记 url 前缀：签名过期最常见，让运维快速判断是签名问题还是资源被删
                    logger.warning("_download_file: 403 Forbidden url_prefix=%s", url_prefix)
                    # 签名 URL 过期
                    raise FileRenewNeeded(url)
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(8192):
                        f.write(chunk)
        except asyncio.TimeoutError:
            # 超时 warning 记 url 前缀：CDN 抖动/带宽打满时会触发，定位是哪条素材慢
            logger.warning("_download_file: timeout url_prefix=%s", url_prefix)
            raise
        except aiohttp.ClientError as e:
            # 网络异常 warning 记 url 前缀：DNS/连接重置/SSL 问题，区分 403 和超时
            logger.warning("_download_file: client error url_prefix=%s error=%s: %s", url_prefix, type(e).__name__, e)
            raise NetworkError(f"Download failed: {e}") from e
        return dest

    # ------------------------------------------------------------------
    # 构建 UploadRequest
    # ------------------------------------------------------------------
    def _build_request(
        self,
        req_cls: type,
        content_type: str,
        account_name: str,
        task: dict,
        files: list[Path],
    ) -> Any:
        """根据 Request dataclass 构建请求对象。"""
        title = task.get("title", "")
        tags = task.get("tags", [])
        description = task.get("description", "")

        # 根据 dataclass 字段适配
        if content_type == "video":
            return req_cls(
                account_name=account_name,
                video_file=files[0] if files else None,
                title=title,
                tags=tags,
                publish_date=None,
                headless=True,
                debug=False,
            )
        else:
            # note / 图文
            return req_cls(
                account_name=account_name,
                image_files=files,
                title=title or description,
                tags=tags,
                publish_date=None,
                headless=True,
                debug=False,
            )

    # ------------------------------------------------------------------
    # 恢复与持久化
    # ------------------------------------------------------------------
    async def recover_pending(self) -> None:
        """
        服务重启时扫描 local_tasks 中 queued/running 的任务，重新提交执行。
        """
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT payload FROM local_tasks WHERE status IN ('queued', 'running') ORDER BY created_at"
            ).fetchall()
            row_count = len(rows)
            # 开始 info 行数：服务重启后能直观看到有多少任务需要恢复，0 条就跳过后面
            logger.info("recover_pending: start, pending rows=%d", row_count)
            recovered = 0
            for row in rows:
                task = json.loads(row["payload"])
                task_id = task["task_id"]
                logger.info("Recovering pending task: %s", task_id)
                # 重置为 queued
                self._update_task_status(task_id, "queued")
                self.submit(task)
                recovered += 1
            # 结束 info 已恢复多少：和开始行数对比，确认没有任务在 submit 阶段抛错被漏掉
            logger.info("recover_pending: done, recovered=%d / %d", recovered, row_count)
        finally:
            conn.close()

    async def _recover_queued(self) -> None:
        """恢复时扫描 queued 任务并执行。"""
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT payload FROM local_tasks WHERE status = 'queued' ORDER BY created_at"
            ).fetchall()
            row_count = len(rows)
            # 开始 info 行数：暂停恢复后触发的 queued 扫描，和 recover_pending 区分开
            logger.info("_recover_queued: start, queued rows=%d", row_count)
            recovered = 0
            for row in rows:
                task = json.loads(row["payload"])
                task_id = task["task_id"]
                logger.info("Resuming queued task: %s", task_id)
                # 取消已存在的同名任务
                existing = self._active_tasks.pop(task_id, None)
                if existing and not existing.done():
                    existing.cancel()
                aio_task = asyncio.create_task(self._run_with_semaphore(task))
                self._active_tasks[task_id] = aio_task
                aio_task.add_done_callback(lambda t: self._active_tasks.pop(task_id, None))
                recovered += 1
            # 结束 info 已恢复多少：确认 resume 过程没丢任务
            logger.info("_recover_queued: done, resumed=%d / %d", recovered, row_count)
        finally:
            conn.close()

    def _save_task(self, task: dict, status: str) -> None:
        """任务落 local_tasks 表。"""
        conn = get_connection()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO local_tasks (task_id, payload, status, created_at, updated_at)
                   VALUES (?, ?, ?, datetime('now'), datetime('now'))""",
                (task["task_id"], json.dumps(task, ensure_ascii=False), status),
            )
            conn.commit()
        finally:
            conn.close()

    def _update_task_status(self, task_id: str, status: str) -> None:
        """更新 local_tasks 表状态。"""
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE local_tasks SET status = ?, updated_at = datetime('now') WHERE task_id = ?",
                (status, task_id),
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 回调上报
    # ------------------------------------------------------------------
    async def _report_progress(self, task_id: str, stage: str, percent: int, message: str) -> None:
        if self._progress_cb:
            try:
                await self._progress_cb(task_id, stage, percent, message)
            except Exception:
                logger.exception("Error reporting progress for %s", task_id)

    async def _report_result(
        self, task_id: str, status: str, error: str | None, publish_url: str | None
    ) -> None:
        if self._result_cb:
            try:
                await self._result_cb(task_id, status, error, publish_url)
            except Exception:
                logger.exception("Error reporting result for %s", task_id)

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------
    def _cleanup_downloads(self, task_id: str) -> None:
        """任务完成后清理下载文件。"""
        dest_dir = _DOWNLOADS_DIR / task_id
        if dest_dir.exists():
            try:
                shutil.rmtree(dest_dir)
                # 成功 info：带 task_id，确认磁盘空间已释放
                logger.info("_cleanup_downloads: success task_id=%s dir=%s", task_id, dest_dir)
                logger.debug("Cleaned up downloads for task %s", task_id)
            except OSError as e:
                # 失败 info：文件被锁/权限问题，可能导致磁盘泄漏需要人工处理
                logger.info("_cleanup_downloads: failed task_id=%s dir=%s error=%s", task_id, dest_dir, e)
                logger.warning("Failed to clean up downloads for task %s", task_id)

    # ------------------------------------------------------------------
    # 异常分类
    # ------------------------------------------------------------------
    @staticmethod
    def _is_cookie_error(e: Exception) -> bool:
        """判断是否为 cookie 失效错误。"""
        msg = str(e).lower()
        return any(kw in msg for kw in ("cookie", "login", "auth", "session expired", "not logged in"))

    @staticmethod
    def _is_network_error(e: Exception) -> bool:
        """判断是否为网络错误。"""
        if isinstance(e, (aiohttp.ClientError, asyncio.TimeoutError)):
            return True
        msg = str(e).lower()
        return any(kw in msg for kw in ("connection", "timeout", "network", "dns", "socket"))


# ---------------------------------------------------------------------------
# 自定义异常
# ---------------------------------------------------------------------------
class FileRenewNeeded(Exception):
    """素材签名 URL 过期，需要请求重签。"""
    pass


class NetworkError(Exception):
    """网络错误。"""
    pass


def _guess_extension(url: str, default: str = "jpg") -> str:
    """从 URL 猜测文件扩展名。"""
    path = url.split("?")[0]
    if "." in path.split("/")[-1]:
        ext = path.rsplit(".", 1)[-1].lower()
        if ext in ("jpg", "jpeg", "png", "gif", "webp", "bmp"):
            return ext
    return default
