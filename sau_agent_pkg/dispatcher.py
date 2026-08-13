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

        # 更新状态为 running
        self._update_task_status(task_id, "running")
        await self._report_progress(task_id, "downloading", 0, "Starting download")

        try:
            # 1. 解析账号
            account_name = task.get("account_name") or await accounts.first_valid(platform_key)
            if not account_name:
                await self._report_result(task_id, "failed", "cookie missing/expired", None)
                self._update_task_status(task_id, "failed")
                return

            # 2. 检查 cookie 有效性
            caps = PLATFORMS.get(platform_key)
            if not caps:
                await self._report_result(task_id, "failed", f"Unknown platform: {platform_key}", None)
                self._update_task_status(task_id, "failed")
                return

            if not await accounts.check_validity(platform_key, account_name):
                await self._report_result(task_id, "failed", "cookie missing/expired", None)
                self._update_task_status(task_id, "failed")
                return

            # 3. 下载素材
            dest_dir = _DOWNLOADS_DIR / task_id
            dest_dir.mkdir(parents=True, exist_ok=True)

            try:
                files = await self._download_materials(task, dest_dir)
            except FileRenewNeeded:
                # 签名 URL 过期，请求重签
                if self._file_renew_cb:
                    await self._file_renew_cb(task_id)
                await self._report_progress(task_id, "downloading", 0, "Waiting for file renew")
                return

            await self._report_progress(task_id, "uploading", 50, "Upload starting")

            # 4. 构建 UploadRequest
            req_cls = get_request_class(platform_key, content_type)
            upload_fn = get_upload_fn(platform_key, content_type)
            if not req_cls or not upload_fn:
                await self._report_result(
                    task_id, "failed",
                    f"Platform {platform_key} does not support {content_type}",
                    None,
                )
                self._update_task_status(task_id, "failed")
                return

            req = self._build_request(req_cls, content_type, account_name, task, files)

            # 5. 执行上传
            await self._report_progress(task_id, "publishing", 70, "Uploading to platform")
            await upload_fn(req)

            # 6. 成功
            await self._report_progress(task_id, "publishing", 100, "Upload complete")
            await self._report_result(task_id, "success", None, None)
            self._update_task_status(task_id, "success")
            logger.info("Task %s completed successfully", task_id)

        except Exception as e:
            error_msg = str(e)
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
        dest = dest_dir / filename
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                if resp.status == 403:
                    # 签名 URL 过期
                    raise FileRenewNeeded(url)
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(8192):
                        f.write(chunk)
        except aiohttp.ClientError as e:
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
            for row in rows:
                task = json.loads(row["payload"])
                task_id = task["task_id"]
                logger.info("Recovering pending task: %s", task_id)
                # 重置为 queued
                self._update_task_status(task_id, "queued")
                self.submit(task)
        finally:
            conn.close()

    async def _recover_queued(self) -> None:
        """恢复时扫描 queued 任务并执行。"""
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT payload FROM local_tasks WHERE status = 'queued' ORDER BY created_at"
            ).fetchall()
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
                logger.debug("Cleaned up downloads for task %s", task_id)
            except OSError:
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
