"""
sau_agent_pkg.updater
~~~~~~~~~~~~~~~~~~~~~
半自动更新管理模块（服务侧，无 UI，见设计文档 §9.1 / M5）。

流程：
- 收到服务端 upgrade_notice（data: {version, download_url, file_hash}）
- validate_notice 校验：字段非空、版本号语义化且 > 当前版本、
  download_url 必须 https 且域名 ∈ 白名单
- 后台下载新版本安装包到 %ProgramData%\\SAU\\updates\\sau-{version}.exe
  （1MB 分块流式 + 边下边算 SHA-256，O(1) 内存；.part 临时文件）
- 校验通过 → phase=ready，等待托盘轮询 /upgrade 后弹确认框执行安装

状态文件：%ProgramData%\\SAU\\etc\\upgrade_state.json（原子写 tmp+rename）
phase ∈ noticed/downloading/ready/snoozed/applying/success/failed/rolled_back
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp

from sau_agent_pkg.config import SAU_HOME, load_config
from sau_agent_pkg.version import APP_VERSION

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
UPDATES_DIR: Path = SAU_HOME / "updates"
BACKUP_DIR: Path = UPDATES_DIR / "backup"
RUNNER_DIR: Path = UPDATES_DIR / "runner"
STATE_FILE: Path = SAU_HOME / "etc" / "upgrade_state.json"
BACKUP_META_FILE: Path = BACKUP_DIR / "backup_meta.json"

_CHUNK_SIZE = 1024 * 1024          # 1MB 分块
_DOWNLOAD_TIMEOUT = 1800           # 单次下载总超时（秒，安装包约 200MB+）
_MAX_ATTEMPTS = 3                  # 失败重试次数（指数退避）
_BACKOFF_INITIAL = 2               # 重试初始退避（秒）
BACKUP_RETENTION_SECONDS = 24 * 3600  # 备份/旧安装包保留 24h

PHASES = (
    "noticed", "downloading", "ready", "snoozed",
    "applying", "success", "failed", "rolled_back",
)

# 单飞锁：防止重复推送触发并发下载
_download_lock = asyncio.Lock()
# 后台下载任务引用（防止被 GC 回收；handle_upgrade_notice 任务化下载后立即返回）
_download_task: Optional[asyncio.Task] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 状态文件读写（原子写 tmp+rename）
# ---------------------------------------------------------------------------
def load_upgrade_state() -> Optional[dict[str, Any]]:
    """读取 upgrade_state.json；不存在/损坏返回 None。"""
    if not STATE_FILE.exists():
        return None
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        logger.warning("Corrupted upgrade state file: %s", STATE_FILE)
        return None


def save_upgrade_state(**fields: Any) -> dict[str, Any]:
    """合并更新状态字段并原子写回（tmp + os.replace）。"""
    state = load_upgrade_state() or {}
    state.update(fields)
    state["updated_at"] = _now_iso()

    phase = state.get("phase")
    if phase and phase not in PHASES:
        logger.warning("Unknown upgrade phase: %s", phase)

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)
    logger.debug("Upgrade state saved: phase=%s version=%s", state.get("phase"), state.get("version"))
    return state


# ---------------------------------------------------------------------------
# 版本比较（自带元组比较，不依赖第三方）
# ---------------------------------------------------------------------------
def parse_version(version: str) -> Optional[tuple[int, ...]]:
    """解析语义化版本号为整数元组；非法返回 None。

    允许每段带非数字后缀（如 1.2.0-beta，取前导数字部分）。
    """
    if not version or not isinstance(version, str):
        return None
    parts = version.strip().split(".")
    if not parts:
        return None
    nums: list[int] = []
    for part in parts:
        m = re.match(r"^(\d+)", part)
        if not m:
            return None
        nums.append(int(m.group(1)))
    return tuple(nums)


def is_newer_version(new_version: str, current_version: str) -> bool:
    """new_version 是否严格大于 current_version（元组比较）。"""
    new_t = parse_version(new_version)
    cur_t = parse_version(current_version)
    if new_t is None or cur_t is None:
        return False
    return new_t > cur_t


# ---------------------------------------------------------------------------
# 通知校验
# ---------------------------------------------------------------------------
def _domain_whitelist() -> list[str]:
    """下载域名白名单：配置项 update_domain_whitelist 优先，
    缺省取 config server_url 的 host。"""
    cfg = load_config()
    wl = cfg.get("update_domain_whitelist")
    if wl and isinstance(wl, (list, tuple)):
        return [str(x).strip().lower() for x in wl if str(x).strip()]
    host = urlparse(cfg.get("server_url", "")).hostname
    return [host.lower()] if host else []


def _host_in_whitelist(hostname: str, whitelist: list[str]) -> bool:
    host = hostname.lower()
    for domain in whitelist:
        if host == domain or host.endswith("." + domain):
            return True
    return False


def validate_notice(data: Any) -> tuple[bool, str]:
    """校验 upgrade_notice 消息 data。返回 (是否合法, 原因)。"""
    if not isinstance(data, dict):
        return False, "data is not an object"

    version = str(data.get("version") or "").strip()
    download_url = str(data.get("download_url") or "").strip()
    file_hash = str(data.get("file_hash") or "").strip()

    if not version or not download_url or not file_hash:
        return False, "missing required field(s): version/download_url/file_hash"

    if not re.fullmatch(r"[A-Fa-f0-9]{64}", file_hash):
        return False, "file_hash is not a 64-char sha256 hex"

    if parse_version(version) is None:
        return False, f"invalid semantic version: {version}"

    if not is_newer_version(version, APP_VERSION):
        return False, f"version {version} is not newer than current {APP_VERSION}"

    parsed = urlparse(download_url)
    if parsed.scheme != "https":
        return False, "download_url must be https"

    whitelist = _domain_whitelist()
    if not whitelist:
        return False, "domain whitelist is empty (server_url not configured?)"
    if not parsed.hostname or not _host_in_whitelist(parsed.hostname, whitelist):
        return False, f"host {parsed.hostname!r} not in whitelist {whitelist}"

    return True, ""


# ---------------------------------------------------------------------------
# SHA-256
# ---------------------------------------------------------------------------
def sha256_of_file(path: Path, chunk_size: int = _CHUNK_SIZE) -> str:
    """流式计算文件 SHA-256（O(1) 内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------
async def _download_once(url: str, part_path: Path) -> str:
    """单次流式下载（1MB 分块，边下边算 SHA-256），返回摘要。

    镜像 dispatcher._download_file 的 aiohttp 分块下载模式。
    """
    part_path.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT)
        ) as resp:
            resp.raise_for_status()
            with open(part_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(_CHUNK_SIZE):
                    f.write(chunk)
                    h.update(chunk)
    return h.hexdigest()


async def download_installer() -> Optional[Path]:
    """下载安装包（单飞：已有下载进行中则直接跳过）。

    成功返回安装包路径并置 phase=ready；失败/校验不通过返回 None。
    """
    if _download_lock.locked():
        logger.info("Installer download already in progress, skipping duplicate")
        return None

    async with _download_lock:
        state = load_upgrade_state() or {}
        version = str(state.get("version") or "")
        url = str(state.get("download_url") or "")
        expected_hash = str(state.get("file_hash") or "").lower()
        if not version or not url or not expected_hash:
            logger.warning("Upgrade state incomplete, cannot download")
            return None

        target = UPDATES_DIR / f"sau-{version}.exe"

        # 已存在且 hash 匹配 → 跳过
        if target.exists():
            try:
                if sha256_of_file(target) == expected_hash:
                    save_upgrade_state(phase="ready", installer_path=str(target))
                    logger.info("Installer already exists with matching hash: %s", target)
                    return target
                logger.warning("Existing installer hash mismatch, re-downloading: %s", target)
            except OSError as e:
                logger.warning("Failed to hash existing installer (%s), re-downloading", e)

        part_path = target.parent / (target.name + ".part")
        delay = _BACKOFF_INITIAL
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                save_upgrade_state(phase="downloading", installer_path=str(target))
                logger.info("Downloading installer (attempt %d/%d): %s", attempt, _MAX_ATTEMPTS, url)
                digest = await _download_once(url, part_path)

                if digest != expected_hash:
                    # SHA-256 不匹配：删除文件并记日志，等待下次推送（不重试）
                    try:
                        part_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    logger.error(
                        "Installer SHA-256 mismatch: expected=%s actual=%s (discarded, waiting for next notice)",
                        expected_hash, digest,
                    )
                    save_upgrade_state(phase="noticed")
                    return None

                os.replace(part_path, target)
                save_upgrade_state(phase="ready", installer_path=str(target))
                logger.info("Installer downloaded and verified: %s", target)
                return target

            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                logger.warning("Installer download attempt %d failed: %s", attempt, e)
                try:
                    part_path.unlink(missing_ok=True)
                except OSError:
                    pass
                if attempt < _MAX_ATTEMPTS:
                    logger.info("Retrying in %ds...", delay)
                    await asyncio.sleep(delay)
                    delay *= 2

        logger.error("Installer download failed after %d attempts (version=%s)", _MAX_ATTEMPTS, version)
        save_upgrade_state(phase="noticed")
        return None


# ---------------------------------------------------------------------------
# upgrade_notice 入口（接线到 SauAgentCore.on_upgrade_notice）
# ---------------------------------------------------------------------------
def _on_download_task_done(task: asyncio.Task) -> None:
    """后台下载任务异常兜底日志（避免 'Task exception was never retrieved'）。"""
    try:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Background installer download task failed: %s", exc)
    except Exception:
        logger.exception("Unexpected error while handling download task result")


async def handle_upgrade_notice(data: dict) -> None:
    """处理服务端推送的 upgrade_notice（core._route_message 传入 msg["data"]）。"""
    ok, reason = validate_notice(data)
    if not ok:
        logger.warning("Upgrade notice rejected: %s", reason)
        return

    version = str(data["version"]).strip()
    download_url = str(data["download_url"]).strip()
    file_hash = str(data["file_hash"]).strip().lower()

    state = load_upgrade_state() or {}
    if state.get("version") == version and state.get("phase") in ("ready", "applying", "success"):
        logger.info("Version %s already in phase=%s, skipping", version, state.get("phase"))
        return

    save_upgrade_state(
        phase="noticed",
        version=version,
        download_url=download_url,
        file_hash=file_hash,
        installer_path=str(UPDATES_DIR / f"sau-{version}.exe"),
        noticed_at=_now_iso(),
    )
    logger.info("Upgrade notice accepted: version=%s", version)

    # 下载任务化为后台任务：不得阻塞 WS 消息接收循环（下载可达数十分钟，
    # 期间 publish_task/heartbeat_ack 等消息不能积压）。
    # _download_lock 单飞锁已防重复下载；模块级引用防止任务被 GC 回收。
    global _download_task
    if _download_task is None or _download_task.done():
        _download_task = asyncio.create_task(download_installer())
        _download_task.add_done_callback(_on_download_task_done)


# ---------------------------------------------------------------------------
# 过期清理（服务启动时调用）
# ---------------------------------------------------------------------------
def _backup_age_seconds() -> Optional[float]:
    """备份年龄（秒）：优先读 backup_meta.json 时间戳，回退目录 mtime。"""
    if not BACKUP_DIR.exists():
        return None
    ts: Optional[float] = None
    if BACKUP_META_FILE.exists():
        try:
            meta = json.loads(BACKUP_META_FILE.read_text(encoding="utf-8"))
            raw = meta.get("timestamp", "")
            ts = datetime.fromisoformat(raw).timestamp()
        except (json.JSONDecodeError, ValueError, OSError):
            ts = None
    if ts is None:
        try:
            ts = BACKUP_DIR.stat().st_mtime
        except OSError:
            return None
    return time.time() - ts


def cleanup_expired() -> None:
    """清理 >24h 的 backup/ 与旧安装包（同步，服务启动时调用）。"""
    # 1. 备份目录
    age = _backup_age_seconds()
    if age is not None and age > BACKUP_RETENTION_SECONDS:
        try:
            shutil.rmtree(BACKUP_DIR, ignore_errors=True)
            logger.info("Expired backup removed (age=%.1fh)", age / 3600)
        except Exception:
            logger.exception("Failed to remove expired backup")

    # 2. 旧安装包 / 残留 .part（>24h，且不是当前待安装文件）
    if not UPDATES_DIR.exists():
        return
    state = load_upgrade_state() or {}
    keep_path: Optional[str] = None
    if state.get("phase") in ("downloading", "ready", "applying"):
        keep_path = state.get("installer_path")
    now = time.time()
    for f in UPDATES_DIR.iterdir():
        if not f.is_file():
            continue
        if not (f.name.startswith("sau-") and (f.name.endswith(".exe") or f.name.endswith(".exe.part"))):
            continue
        if keep_path:
            try:
                if f.samefile(Path(keep_path)):
                    continue  # 当前待安装文件不清理
            except OSError:
                pass
        try:
            if now - f.stat().st_mtime > BACKUP_RETENTION_SECONDS:
                f.unlink()
                logger.info("Expired installer removed: %s", f)
        except OSError:
            logger.debug("Failed to remove expired file: %s", f)
