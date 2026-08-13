#!/usr/bin/env python3
"""
SAU Local Agent — WebSocket client that connects to opcgeo server
to receive and execute publish tasks.

Architecture: SAU runs on the user's local machine, connects OUT to the
opcgeo server via WebSocket, and executes video upload tasks using the
same uploader/ system as the CLI.

Usage:
    python sau_agent.py
    # or with env vars:
    SAU_SERVER_WS=ws://host:8888/opcgeo/agent/ws SAU_AGENT_TOKEN=xxx python sau_agent.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
import os
import sys
import requests as http_requests
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    import websockets
except ImportError:
    print("[Agent] ERROR: 'websockets' package not installed. Run: pip install websockets")
    sys.exit(1)

from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidHandshake

from sau_agent_pkg.version import APP_VERSION


# ---------------------------------------------------------------------------
# 日志（sau_agent.py 旧版入口独立日志）
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)


def _setup_agent_logging() -> None:
    """初始化 sau_agent 的日志（与 sau_agent_pkg.core 语义对齐）。"""
    try:
        root = logging.getLogger()
        if root.handlers:
            return
        root.setLevel(logging.INFO)

        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(ch)

        try:
            log_dir = Path(__file__).resolve().parent / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                str(log_dir / "sau-agent-legacy.log"),
                maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
            )
            fh.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            root.addHandler(fh)
        except Exception:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cookie / account helpers (mirrors sau_cli.py resolve_account_file)
# ---------------------------------------------------------------------------

COOKIES_DIR = Path(__file__).parent / "cookies"
UPLOADER_DIR = Path(__file__).parent / "uploader"

# platform_key -> cookie-file prefix (matches sau_backend.py V2_PLATFORM_COOKIE_PREFIX)
PLATFORM_COOKIE_PREFIX = {
    "douyin": "douyin",
    "xiaohongshu": "xiaohongshu",
    "shipinhao": "tencent",
    "bilibili": "bilibili",
    "baijiahao": "baijiahao",
    "kuaishou": "kuaishou",
}


def _resolve_account_file(platform_key: str, account_name: str = "default") -> Path:
    """Return cookie file path for a given platform + account."""
    prefix = PLATFORM_COOKIE_PREFIX.get(platform_key, platform_key)
    return COOKIES_DIR / f"{prefix}_{account_name}.json"


def _find_default_account_name(platform_key: str) -> str:
    """Scan cookies dir for the first account of this platform, fallback to 'default'."""
    prefix = PLATFORM_COOKIE_PREFIX.get(platform_key, platform_key)
    if COOKIES_DIR.exists():
        for f in COOKIES_DIR.iterdir():
            if f.name.startswith(f"{prefix}_") and f.name.endswith(".json"):
                stem = f.stem  # e.g. "douyin_myacc"
                return stem[len(prefix) + 1:]
    return "default"


# ---------------------------------------------------------------------------
# Dynamic platform discovery from uploader/ directory
# ---------------------------------------------------------------------------

def _discover_uploader_dirs() -> list[str]:
    """Scan uploader/ for subdirectories that look like platform uploaders.
    Returns directory names like ['douyin_uploader', 'ks_uploader', ...].
    """
    if not UPLOADER_DIR.exists():
        return []
    dirs = []
    for entry in UPLOADER_DIR.iterdir():
        if entry.is_dir() and (entry / "main.py").exists():
            dirs.append(entry.name)
    return dirs


def _dir_name_to_platform_key(dir_name: str) -> str:
    """Convert uploader dir name to platform_key.
    E.g. 'douyin_uploader' -> 'douyin', 'ks_uploader' -> 'ks',
         'tencent_uploader' -> 'shipinhao' (special mapping).
    """
    # Strip _uploader / _upl suffix
    name = dir_name
    for suffix in ("_uploader", "_upl"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    # Special mappings
    SPECIAL = {"tencent": "shipinhao", "xhs": "xiaohongshu"}
    return SPECIAL.get(name, name)


def discover_available_platforms() -> list[str]:
    """Return list of platform_keys available on this machine.
    Combines the known PLATFORM_COOKIE_PREFIX keys with any extra uploader
    directories found in uploader/.
    """
    known = set(PLATFORM_COOKIE_PREFIX.keys())
    for dir_name in _discover_uploader_dirs():
        pk = _dir_name_to_platform_key(dir_name)
        known.add(pk)
    # Always include bilibili (CLI-based, no main.py pattern needed)
    known.add("bilibili")
    return sorted(known)


# ---------------------------------------------------------------------------
# Upload execution — known platforms + dynamic fallback
# ---------------------------------------------------------------------------

async def _run_upload(platform_key: str, account_file: Path, title: str,
                      file_path: str, tags: list, description: str,
                      publish_date) -> None:
    """
    Execute the actual upload for a given platform.
    Uses a registry for known platforms with specific call patterns,
    and falls back to dynamic discovery for unknown platforms.
    """

    # --- bilibili: CLI-based (biliup) ---
    if platform_key == "bilibili":
        from uploader.bilibili_uploader.runtime import run_biliup_command
        if not account_file.exists():
            raise RuntimeError(
                f"Bilibili account file is missing: {account_file}. "
                f"Run `sau bilibili login` first."
            )
        arguments = [
            "-u", str(account_file), "upload",
            str(file_path), "--title", title,
            "--desc", description or "", "--tid", "130",
        ]
        if tags:
            arguments.extend(["--tag", ",".join(tags)])
        if publish_date and publish_date != 0:
            if isinstance(publish_date, datetime):
                arguments.extend(["--dtime", str(int(publish_date.timestamp()))])
        result = run_biliup_command(arguments)
        if result.returncode != 0:
            raise RuntimeError(
                (result.stderr or result.stdout or "").strip() or "Bilibili upload failed"
            )
        return

    # --- Try known platform handlers first ---
    handler = _KNOWN_PLATFORMS.get(platform_key)
    if handler:
        await handler(account_file, title, file_path, tags, description, publish_date)
        return

    # --- Dynamic fallback: find uploader module and try generic pattern ---
    await _run_upload_dynamic(platform_key, account_file, title, file_path,
                              tags, description, publish_date)


# ---------------------------------------------------------------------------
# Known platform handlers (registered individually)
# ---------------------------------------------------------------------------

async def _upload_douyin(account_file, title, file_path, tags, description, publish_date):
    # 为什么：每个平台上传函数入口 debug 留痕，多路复用时知道走的是哪条路径
    logger.debug("_upload_douyin entry: account_file=%s title_len=%d", account_file, len(title) if title else 0)
    from uploader.douyin_uploader.main import (
        douyin_setup, DouYinVideo, DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
    )
    is_ready = await douyin_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(f"Douyin cookie missing/expired: {account_file}")
    app = DouYinVideo(
        title, str(file_path), tags or [], publish_date or 0,
        str(account_file),
        desc=description or "",
        publish_strategy=DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
        debug=True, headless=True,
    )
    await app.douyin_upload_video()


async def _upload_xiaohongshu(account_file, title, file_path, tags, description, publish_date):
    logger.debug("_upload_xiaohongshu entry: account_file=%s title_len=%d", account_file, len(title) if title else 0)
    from uploader.xiaohongshu_uploader.main import (
        xiaohongshu_setup, XiaoHongShuVideo, XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
    )
    is_ready = await xiaohongshu_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(f"Xiaohongshu cookie missing/expired: {account_file}")
    app = XiaoHongShuVideo(
        title=title, file_path=str(file_path), desc=description or "",
        tags=tags or [], publish_date=publish_date or 0,
        account_file=str(account_file),
        publish_strategy=XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug=True, headless=True,
    )
    await app.main()


async def _upload_shipinhao(account_file, title, file_path, tags, description, publish_date):
    logger.debug("_upload_shipinhao entry: account_file=%s title_len=%d", account_file, len(title) if title else 0)
    from uploader.tencent_uploader.main import (
        tencent_setup, TencentVideo, TENCENT_PUBLISH_STRATEGY_IMMEDIATE,
    )
    is_ready = await tencent_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(f"Tencent/Shipinhao cookie missing/expired: {account_file}")
    app = TencentVideo(
        title=title, file_path=str(file_path), tags=tags or [],
        publish_date=publish_date or 0, account_file=str(account_file),
        desc=description or "",
        publish_strategy=TENCENT_PUBLISH_STRATEGY_IMMEDIATE,
        debug=True, headless=True,
    )
    await app.tencent_upload_video()


async def _upload_kuaishou(account_file, title, file_path, tags, description, publish_date):
    logger.debug("_upload_kuaishou entry: account_file=%s title_len=%d", account_file, len(title) if title else 0)
    from uploader.ks_uploader.main import (
        ks_setup, KSVideo, KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE,
    )
    is_ready = await ks_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(f"Kuaishou cookie missing/expired: {account_file}")
    app = KSVideo(
        title=title, file_path=str(file_path), desc=description or "",
        tags=tags or [], publish_date=publish_date or 0,
        account_file=str(account_file),
        publish_strategy=KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE,
        debug=True, headless=True,
    )
    await app.main()


async def _upload_baijiahao(account_file, title, file_path, tags, description, publish_date):
    logger.debug("_upload_baijiahao entry: account_file=%s title_len=%d", account_file, len(title) if title else 0)
    from uploader.baijiahao_uploader.main import (
        baijiahao_setup, BaiJiaHaoVideo,
    )
    is_ready = await baijiahao_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(f"Baijiahao cookie missing/expired: {account_file}")
    app = BaiJiaHaoVideo(
        title=title, file_path=str(file_path), tags=tags or [],
        publish_date=publish_date or 0, account_file=str(account_file),
    )
    await app.main()


_KNOWN_PLATFORMS = {
    "douyin": _upload_douyin,
    "xiaohongshu": _upload_xiaohongshu,
    "shipinhao": _upload_shipinhao,
    "kuaishou": _upload_kuaishou,
    "baijiahao": _upload_baijiahao,
}


# ---------------------------------------------------------------------------
# Dynamic fallback: try to load any unknown platform from uploader/
# ---------------------------------------------------------------------------

async def _run_upload_dynamic(platform_key: str, account_file: Path, title: str,
                              file_path: str, tags: list, description: str,
                              publish_date) -> None:
    """
    Attempt to upload using a dynamically discovered uploader module.
    Scans uploader/ for a matching directory and tries generic patterns:
      - {name}_setup(account_file, handle=False) -> bool
      - {Name}Video(...) or similar class -> .main()
    """
    import importlib

    # Find matching uploader dir
    target_dir = None
    for dir_name in _discover_uploader_dirs():
        pk = _dir_name_to_platform_key(dir_name)
        if pk == platform_key:
            target_dir = dir_name
            break

    if not target_dir:
        raise ValueError(
            f"Unsupported platform: {platform_key}. "
            f"No uploader module found in uploader/. "
            f"Available: {discover_available_platforms()}"
        )

    # Try to import the module
    module_name = f"uploader.{target_dir}.main"
    # 为什么：动态 fallback 的第一步——模块能否导入决定路径是否正确，debug 记录被试的模块名
    logger.debug("_run_upload_dynamic: importing module=%s for platform=%s", module_name, platform_key)
    try:
        mod = importlib.import_module(module_name)
    except ImportError as e:
        logger.warning("_run_upload_dynamic: module import failed for %s: %s", module_name, e)
        raise ValueError(f"Cannot import {module_name}: {e}")

    # Look for setup function: try {platform_key}_setup, or any *_setup
    setup_fn = None
    for attr_name in dir(mod):
        if attr_name.endswith("_setup") and callable(getattr(mod, attr_name)):
            setup_fn = getattr(mod, attr_name)
            break

    if setup_fn:
        # 为什么：找到 setup 函数意味着动态发现成功了一半，info 记录便于确认路径
        logger.info("_run_upload_dynamic: found setup_fn=%s for platform=%s",
                    getattr(setup_fn, "__name__", repr(setup_fn)), platform_key)
        is_ready = await setup_fn(str(account_file), handle=False)
        if not is_ready:
            raise RuntimeError(
                f"{platform_key} cookie missing/expired: {account_file}"
            )

    # Look for Video class: try {Name}Video, or any class ending with Video
    video_cls = None
    for attr_name in dir(mod):
        if attr_name.endswith("Video") and isinstance(getattr(mod, attr_name), type):
            video_cls = getattr(mod, attr_name)
            break

    if not video_cls:
        raise ValueError(
            f"No Video class found in {module_name}. "
            f"Cannot determine how to upload for {platform_key}."
        )
    # 为什么：找到 Video 类是动态发现的关键节点，info 记录便于排查"找不到 Video 类"
    logger.info("_run_upload_dynamic: found Video class=%s for platform=%s",
                video_cls.__name__, platform_key)

    # Try to instantiate with common kwargs
    try:
        app = video_cls(
            title=title, file_path=str(file_path), tags=tags or [],
            publish_date=publish_date or 0, account_file=str(account_file),
            desc=description or "", debug=True, headless=True,
        )
    except TypeError:
        # Fallback: try without some kwargs
        try:
            app = video_cls(
                title=title, file_path=str(file_path), tags=tags or [],
                publish_date=publish_date or 0, account_file=str(account_file),
            )
        except TypeError:
            logger.warning("_run_upload_dynamic: cannot instantiate %s (signature mismatch)",
                           video_cls.__name__)
            raise ValueError(
                f"Cannot instantiate {video_cls.__name__} for {platform_key}. "
                f"Constructor signature not compatible."
            )

    # Find upload method: try .main(), or {name}_upload_video(), etc.
    upload_method = None
    for method_name in ("main", f"{platform_key}_upload_video", "upload", "run"):
        m = getattr(app, method_name, None)
        if m and callable(m):
            upload_method = m
            break

    if not upload_method:
        logger.warning("_run_upload_dynamic: no upload method found on %s", video_cls.__name__)
        raise ValueError(
            f"No upload method found on {video_cls.__name__} for {platform_key}."
        )
    logger.info("_run_upload_dynamic: calling upload method=%s on %s",
                getattr(upload_method, "__name__", repr(upload_method)), video_cls.__name__)
    try:
        await upload_method()
    except Exception as e:
        # 为什么：dynamic fallback 比静态 handler 更容易失败，warning 分类（但不吞异常）
        logger.warning("_run_upload_dynamic: upload failed for platform=%s via %s: %s",
                       platform_key, upload_method.__name__, e)
        raise


# ---------------------------------------------------------------------------
# WebSocket Agent
# ---------------------------------------------------------------------------

class SAUAgent:
    def __init__(self, config: dict):
        self.config = config
        self.agent_id = config["agent_id"]
        self.ws = None
        self.active_tasks = 0
        self.running = True

    async def start(self):
        """Main loop: connect to opcgeo, register, listen for tasks. Auto-reconnect."""
        # 为什么：connect loop 进入前 info 标记，统计重连次数
        logger.info("SAUAgent.start: connect loop starting (agent_id prefix=%s...)",
                    self.agent_id[:8] if self.agent_id else "(none)")
        _reconnect_backoff = 5
        while self.running:
            try:
                url = self.config["server_url"]
                # 新后端握手要求 URL 带 agentId；本旧版入口无机器码采集能力，
                # 仅附 agentId（服务端握手对 machine 为空有容忍）
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}agentId={self.agent_id}"
                token = self.config["agent_token"]
                # 为什么：每次 WebSocket 连接尝试打 info（URL 脱敏+token 长度）
                logger.info("SAUAgent.start: connecting WS url=%s (agent_token_len=%d)",
                            url, len(token) if token else 0)
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=60,
                    # token 改经 Authorization header 传递（新后端握手协议）
                    additional_headers={"Authorization": f"Bearer {token}"},
                ) as ws:
                    self.ws = ws
                    # 为什么：连接成功打 info，确认握手通过
                    logger.info("SAUAgent.start: WS connected (backoff reset to 5s)")
                    _reconnect_backoff = 5

                    # Register with server — report all available platforms
                    available = discover_available_platforms()
                    # 为什么：register 是服务端识别这个 agent 的第一步，info 留痕
                    logger.info("SAUAgent.start: sending register — version=%s platforms=%s",
                                APP_VERSION, available)
                    await self._send("register", {
                        "agent_id": self.agent_id,
                        "agent_token": self.config["agent_token"],
                        "platforms": available,
                        "version": APP_VERSION,
                    })

                    # Start heartbeat loop
                    asyncio.create_task(self._heartbeat_loop())

                    # Listen for incoming messages
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            await self._handle_message(msg)
                        except json.JSONDecodeError:
                            logger.warning("SAUAgent.start: invalid JSON from server — prefix=%s", raw[:200])

            except ConnectionClosed as e:
                code = getattr(e, "code", None)
                reason = getattr(e, "reason", "")
                if code in (4401, 4403, 4409, 4410):
                    # 为什么：4401/4403 是凭证类拒绝码（与 sau_agent_pkg/core.py 语义一致），停止重连避免互踢
                    logger.warning("ConnectionClosed credential rejected (code=%s reason=%s) — stop reconnecting",
                                   code, reason)
                    self.ws = None
                    return
                # 为什么：普通断连 warning 带 code/reason+回退秒数
                logger.warning("ConnectionClosed code=%s reason=%s — reconnecting backoff=%ss",
                               code, reason, _reconnect_backoff)
                self.ws = None
                await asyncio.sleep(_reconnect_backoff)
                _reconnect_backoff = min(_reconnect_backoff * 2, 60)
            except (InvalidStatus, InvalidHandshake) as e:
                # 为什么：握手失败（HTTP 4xx/升级失败）通常 token 无效，warning 与 core.py 语义一致
                logger.warning("WS handshake rejected: %s — stop reconnecting (check SAU_AGENT_TOKEN)", e)
                self.ws = None
                return
            except (ConnectionRefusedError, OSError) as e:
                # 为什么：网络层错误（服务未监听/断网），warning 记录类型+回退
                logger.warning("Connection refused/OS error (%s) — reconnecting backoff=%ss",
                               type(e).__name__, _reconnect_backoff)
                self.ws = None
                await asyncio.sleep(_reconnect_backoff)
                _reconnect_backoff = min(_reconnect_backoff * 2, 60)
            except Exception as e:
                # 为什么：意料外异常统一 warning，10s 回退（较慢以避免日志风暴）
                logger.warning("SAUAgent.start unexpected error: %s — reconnecting in 10s", e, exc_info=True)
                await asyncio.sleep(10)

    async def _heartbeat_loop(self):
        """Send heartbeat every 30 seconds."""
        while self.ws and self.ws.open:
            try:
                await self._send("heartbeat", {
                    "agent_id": self.agent_id,
                    "active_tasks": self.active_tasks,
                })
            except Exception:
                break
            await asyncio.sleep(30)

    async def _handle_message(self, msg: dict):
        """Route incoming server messages."""
        msg_type = msg.get("type")
        data = msg.get("data", {})
        # 为什么：WS 消息路由入口 info 留痕（只记录 type，不记录敏感 data），排障时看"服务端发了什么指令"
        logger.info("_handle_message routing: msg_type=%s", msg_type)

        if msg_type == "registered":
            logger.info("_handle_message: server registered OK — message=%s", data.get("message", "OK"))
        elif msg_type == "heartbeat_ack":
            pass  # server acknowledged heartbeat
        elif msg_type == "publish_task":
            # 为什么：收到任务是核心业务事件，info 记 task_id+平台（不记 title/内容等敏感字段）
            task_id = data.get("task_id", "unknown")
            platform = data.get("platform_key", "")
            logger.info("_handle_message: received publish_task task_id=%s platform=%s — spawning executor",
                        task_id, platform)
            asyncio.create_task(self._execute_publish(data))
        else:
            logger.warning("_handle_message: unknown msg_type=%s — ignoring", msg_type)

    async def _execute_publish(self, task_data: dict):
        """Execute a publish task received from the server."""
        task_id = task_data.get("task_id", "unknown")
        platform_key = task_data.get("platform_key", "")
        material_id = task_data.get("material_id")
        callback_url = task_data.get("callback_url")
        self.active_tasks += 1
        # 为什么：upload 各阶段 info 留痕，可追踪任务从开始到完成的生命周期
        logger.info("[task_id=%s] _execute_publish start: platform=%s material_id=%s active_tasks=%d",
                    task_id, platform_key, material_id, self.active_tasks)

        try:
            result = await self._do_upload(platform_key, task_data)
            # 为什么：上传成功 info，确认走到了最终回调前
            logger.info("[task_id=%s] _execute_publish upload succeeded, sending task_result success", task_id)
            await self._send("task_result", {
                "task_id": task_id,
                "material_id": material_id,
                "callback_url": callback_url,
                "status": "success",
                "error": None,
                "publish_url": result.get("publish_url"),
            })
            logger.info("[task_id=%s] task succeeded", task_id)
        except Exception as e:
            # 为什么：失败 warning 分类（异常类型名+msg），同时带 trace 便于定位
            logger.warning("[task_id=%s] task failed (%s): %s", task_id, type(e).__name__, e, exc_info=True)
            await self._send("task_result", {
                "task_id": task_id,
                "material_id": material_id,
                "callback_url": callback_url,
                "status": "failed",
                "error": str(e),
                "publish_url": None,
            })
        finally:
            self.active_tasks -= 1
            logger.info("[task_id=%s] _execute_publish finished (active_tasks=%d)",
                        task_id, self.active_tasks)

    async def _do_upload(self, platform_key: str, task_data: dict) -> dict:
        """
        Prepare and execute upload using the uploader/ system.
        Mirrors the exact patterns from sau_cli.py.
        """
        task_id = task_data.get("task_id", "unknown")
        title = task_data.get("title", "")
        tags = task_data.get("tags", [])
        description = task_data.get("description", "")
        file_path = task_data.get("file_path", "")
        file_url = task_data.get("file_url", "")
        scheduled_at = task_data.get("scheduled_at")
        account_name = task_data.get("account_name")
        # 为什么：_do_upload 入口 info，确认解析和调度已完成
        logger.info("[task_id=%s] _do_upload start: platform=%s title_len=%d tags=%d scheduled=%s",
                    task_id, platform_key, len(title) if title else 0, len(tags), bool(scheduled_at))

        # Download file if URL provided and no local path
        if file_url and not file_path:
            logger.info("[task_id=%s] _do_upload: need download file_url, calling _download_file", task_id)
            file_path = self._download_file(file_url)

        if not file_path or not Path(file_path).exists():
            raise Exception(f"File not found: {file_path}")

        # Resolve account (cookie) file
        if not account_name:
            account_name = _find_default_account_name(platform_key)
        # 为什么：账号解析 info（只记账号名+平台，不记 cookie 内容）
        logger.info("[task_id=%s] _do_upload: account resolved platform=%s account=%s",
                    task_id, platform_key, account_name)
        account_file = _resolve_account_file(platform_key, account_name)

        if not account_file.exists():
            # 为什么：cookie 不存在是用户态常见错误（未登录），warning 提示路径
            logger.warning("[task_id=%s] _do_upload: cookie file not found: %s", task_id, account_file)
            raise Exception(
                f"No cookie file for {platform_key}/{account_name}: {account_file}. "
                f"Please login first."
            )
        # 为什么：cookie 文件存在=已通过浏览器登录，info 确认前置条件满足
        logger.info("[task_id=%s] _do_upload: cookie check passed (%s exists)", task_id, account_file)

        # Parse scheduled_at into publish_date
        publish_date = 0
        if scheduled_at:
            try:
                publish_date = datetime.fromisoformat(scheduled_at)
            except (ValueError, TypeError):
                publish_date = 0

        # 为什么：启动浏览器/上传前打 info，知道什么时候真正开始执行上传
        logger.info("[task_id=%s] _do_upload: starting platform uploader (browser launch + upload)...", task_id)
        # Execute upload (same patterns as sau_cli.py)
        try:
            await _run_upload(
                platform_key, account_file, title, file_path,
                tags, description, publish_date,
            )
        except Exception as e:
            # 为什么：上传内部失败 warning 分类（区分 cookie/网络/平台风控）
            logger.warning("[task_id=%s] _run_upload raised %s: %s",
                           task_id, type(e).__name__, e)
            raise
        # 为什么：上传成功 info 配对"启动浏览器"日志
        logger.info("[task_id=%s] _do_upload: upload flow completed successfully", task_id)

        return {"publish_url": None}

    def _download_file(self, url: str) -> str:
        """Download file from URL to local temp directory."""
        temp_dir = Path("temp")
        temp_dir.mkdir(exist_ok=True)
        filename = url.split("/")[-1].split("?")[0] or f"file_{uuid.uuid4().hex[:8]}"
        local_path = temp_dir / filename

        logger.info("_download_file: url=%s -> local=%s", url, local_path)
        resp = http_requests.get(url, stream=True, timeout=300)
        resp.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return str(local_path)

    async def _send(self, msg_type: str, data: dict):
        """Send JSON message through WebSocket."""
        if self.ws and self.ws.open:
            await self.ws.send(json.dumps({"type": msg_type, "data": data}))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Entry point for the SAU WebSocket Agent."""
    _setup_agent_logging()
    # 为什么：解析命令行 info——虽然该入口主要靠环境变量，但留痕便于排障
    logger.info("main: parsing CLI args (sau_agent.py legacy mode). argv=%s", sys.argv[1:])
    logger.warning("main: sau_agent.py is DEPRECATED — users should prefer sau-service")
    config = {
        "server_url": os.environ.get(
            "SAU_SERVER_WS", "ws://127.0.0.1:8888/opcgeo/agent/ws"
        ),
        "agent_token": os.environ.get("SAU_AGENT_TOKEN", ""),
        "agent_id": os.environ.get(
            "SAU_AGENT_ID", f"sau_{uuid.uuid4().hex[:16]}"
        ),
    }

    if not config["agent_token"]:
        logger.warning("main: no SAU_AGENT_TOKEN env var set")

    available = discover_available_platforms()
    # 为什么：main 启动 info（版本+URL+账号扫描数）——确认入口参数
    logger.info("main: SAU Agent legacy starting — version=%s server_url=%s scanned_platforms=%d (agent_id_prefix=%s...)",
                APP_VERSION, config["server_url"], len(available),
                config["agent_id"][:8] if config["agent_id"] else "")
    logger.info("main: available platforms=%s", available)

    agent = SAUAgent(config)
    try:
        asyncio.run(agent.start())
    except KeyboardInterrupt:
        logger.info("main: KeyboardInterrupt — shutting down gracefully")
        agent.running = False


if __name__ == "__main__":
    main()
