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

import asyncio
import json
import uuid
import os
import sys
import requests as http_requests
from datetime import datetime
from pathlib import Path

try:
    import websockets
except ImportError:
    print("[Agent] ERROR: 'websockets' package not installed. Run: pip install websockets")
    sys.exit(1)

from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidHandshake

from sau_agent_pkg.version import APP_VERSION


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
    try:
        mod = importlib.import_module(module_name)
    except ImportError as e:
        raise ValueError(f"Cannot import {module_name}: {e}")

    # Look for setup function: try {platform_key}_setup, or any *_setup
    setup_fn = None
    for attr_name in dir(mod):
        if attr_name.endswith("_setup") and callable(getattr(mod, attr_name)):
            setup_fn = getattr(mod, attr_name)
            break

    if setup_fn:
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
        raise ValueError(
            f"No upload method found on {video_cls.__name__} for {platform_key}."
        )

    await upload_method()


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
        while self.running:
            try:
                url = self.config["server_url"]
                # 新后端握手要求 URL 带 agentId；本旧版入口无机器码采集能力，
                # 仅附 agentId（服务端握手对 machine 为空有容忍）
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}agentId={self.agent_id}"
                print(f"[Agent] Connecting to {url} ...")
                token = self.config["agent_token"]
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=60,
                    # token 改经 Authorization header 传递（新后端握手协议）
                    additional_headers={"Authorization": f"Bearer {token}"},
                ) as ws:
                    self.ws = ws
                    print("[Agent] Connected!")

                    # Register with server — report all available platforms
                    available = discover_available_platforms()
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
                            print(f"[Agent] Invalid JSON received: {raw[:200]}")

            except ConnectionClosed as e:
                code = getattr(e, "code", None)
                if code in (4401, 4403, 4409, 4410):
                    # 凭证类关闭：停止重连，避免对新后端的互踢风暴
                    print(f"[Agent] ERROR: credential rejected by server "
                          f"(close code {code}), stop reconnecting. "
                          f"Please check token / use sau-service instead.")
                    self.ws = None
                    return
                print(f"[Agent] Connection lost ({e}). Reconnecting in 5s...")
                self.ws = None
                await asyncio.sleep(5)
            except (InvalidStatus, InvalidHandshake) as e:
                # 握手被拒（通常 token 无效/过期）：停止重连
                print(f"[Agent] ERROR: WebSocket handshake rejected ({e}), "
                      f"stop reconnecting. Please check SAU_AGENT_TOKEN.")
                self.ws = None
                return
            except (ConnectionRefusedError, OSError) as e:
                print(f"[Agent] Connection lost ({e}). Reconnecting in 5s...")
                self.ws = None
                await asyncio.sleep(5)
            except Exception as e:
                print(f"[Agent] Unexpected error: {e}. Reconnecting in 10s...")
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

        if msg_type == "registered":
            print(f"[Agent] Registered: {data.get('message', 'OK')}")
        elif msg_type == "heartbeat_ack":
            pass  # server acknowledged heartbeat
        elif msg_type == "publish_task":
            asyncio.create_task(self._execute_publish(data))
        else:
            print(f"[Agent] Unknown message type: {msg_type}")

    async def _execute_publish(self, task_data: dict):
        """Execute a publish task received from the server."""
        task_id = task_data.get("task_id", "unknown")
        platform_key = task_data.get("platform_key", "")
        material_id = task_data.get("material_id")
        callback_url = task_data.get("callback_url")
        self.active_tasks += 1
        print(f"[Agent] Executing publish task {task_id} for {platform_key}"
              f" (material_id={material_id})")

        try:
            result = await self._do_upload(platform_key, task_data)
            await self._send("task_result", {
                "task_id": task_id,
                "material_id": material_id,
                "callback_url": callback_url,
                "status": "success",
                "error": None,
                "publish_url": result.get("publish_url"),
            })
            print(f"[Agent] Task {task_id} succeeded")
        except Exception as e:
            await self._send("task_result", {
                "task_id": task_id,
                "material_id": material_id,
                "callback_url": callback_url,
                "status": "failed",
                "error": str(e),
                "publish_url": None,
            })
            print(f"[Agent] Task {task_id} failed: {e}")
        finally:
            self.active_tasks -= 1

    async def _do_upload(self, platform_key: str, task_data: dict) -> dict:
        """
        Prepare and execute upload using the uploader/ system.
        Mirrors the exact patterns from sau_cli.py.
        """
        title = task_data.get("title", "")
        tags = task_data.get("tags", [])
        description = task_data.get("description", "")
        file_path = task_data.get("file_path", "")
        file_url = task_data.get("file_url", "")
        scheduled_at = task_data.get("scheduled_at")
        account_name = task_data.get("account_name")

        # Download file if URL provided and no local path
        if file_url and not file_path:
            file_path = self._download_file(file_url)

        if not file_path or not Path(file_path).exists():
            raise Exception(f"File not found: {file_path}")

        # Resolve account (cookie) file
        if not account_name:
            account_name = _find_default_account_name(platform_key)
        account_file = _resolve_account_file(platform_key, account_name)

        if not account_file.exists():
            raise Exception(
                f"No cookie file for {platform_key}/{account_name}: {account_file}. "
                f"Please login first."
            )

        # Parse scheduled_at into publish_date
        publish_date = 0
        if scheduled_at:
            try:
                publish_date = datetime.fromisoformat(scheduled_at)
            except (ValueError, TypeError):
                publish_date = 0

        # Execute upload (same patterns as sau_cli.py)
        await _run_upload(
            platform_key, account_file, title, file_path,
            tags, description, publish_date,
        )

        return {"publish_url": None}

    def _download_file(self, url: str) -> str:
        """Download file from URL to local temp directory."""
        temp_dir = Path("temp")
        temp_dir.mkdir(exist_ok=True)
        filename = url.split("/")[-1].split("?")[0] or f"file_{uuid.uuid4().hex[:8]}"
        local_path = temp_dir / filename

        print(f"[Agent] Downloading {url} -> {local_path}")
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
    print("[Agent] WARNING: sau_agent.py is DEPRECATED. Please use sau-service instead.")
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
        print("[Agent] WARNING: No agent token set. Use SAU_AGENT_TOKEN env var.")

    available = discover_available_platforms()
    print(f"[Agent] SAU Agent starting (id={config['agent_id']})")
    print(f"[Agent] Server: {config['server_url']}")
    print(f"[Agent] Available platforms: {available}")

    agent = SAUAgent(config)
    try:
        asyncio.run(agent.start())
    except KeyboardInterrupt:
        print("\n[Agent] Shutting down...")
        agent.running = False


if __name__ == "__main__":
    main()
