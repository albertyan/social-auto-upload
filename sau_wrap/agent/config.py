# -*- coding: utf-8 -*-
"""绑定配置与凭证存取（现状文档 §5.8 凭证、§3.6 数据布局）。

- ``config.json``：``server_url``（WS 基址，如 ``wss://host/opcgeo/agent/ws``）与
  ``agent_id``（32 位 UUID hex，bind 时缺失则自动生成）；原子写（临时文件 + os.replace）；
- ``credential.bin``：Agent token，DPAPI **LOCAL_MACHINE** 作用域加密
  （ctypes ``CryptProtectData`` / ``CryptUnprotectData``，不依赖 pywin32 win32crypt）。

``sau bind --server <url> --token <token>`` 的真实实现即 :func:`bind`。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import uuid
from dataclasses import dataclass

from sau_wrap import paths

#: DPAPI 作用域：本机（服务以 SYSTEM 运行亦可解密；用户级无法跨会话，见 §5.8）
_CRYPTPROTECT_LOCAL_MACHINE = 0x04

#: use_last_error=True：配合 ctypes.get_last_error() 取准确错误码（线程安全）
_CRYPT32 = ctypes.WinDLL("crypt32", use_last_error=True)
_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob_from_bytes(data: bytes) -> _DataBlob:
    blob = _DataBlob()
    blob.cbData = len(data)
    blob.pbData = ctypes.create_string_buffer(data, len(data)) if data else None
    return blob


def _dpapi_protect(plain: bytes) -> bytes:
    """DPAPI 加密（LOCAL_MACHINE）。"""
    in_blob = _blob_from_bytes(plain)
    out_blob = _DataBlob()
    ok = _CRYPT32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_LOCAL_MACHINE,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise RuntimeError(f"CryptProtectData 失败: {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _KERNEL32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(cipher: bytes) -> bytes:
    """DPAPI 解密（LOCAL_MACHINE）。"""
    in_blob = _blob_from_bytes(cipher)
    out_blob = _DataBlob()
    ok = _CRYPT32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_LOCAL_MACHINE,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise RuntimeError(f"CryptUnprotectData 失败: {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _KERNEL32.LocalFree(out_blob.pbData)


def _atomic_write(path, data: bytes) -> None:
    paths.ensure_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 配置读写


@dataclass
class AgentConfig:
    """绑定配置（config.json 解析结果）。"""

    server_url: str
    agent_id: str
    heartbeat_interval: int = 30


def load_config() -> AgentConfig | None:
    """读取 config.json；不存在或非法返回 None（调用方据此提示 bind）。"""
    try:
        raw = paths.CONFIG_FILE.read_text(encoding="utf-8")
        obj = json.loads(raw)
    except (OSError, ValueError):
        return None
    server_url = str(obj.get("server_url") or "").strip()
    agent_id = str(obj.get("agent_id") or "").strip()
    if not server_url or not agent_id:
        return None
    try:
        interval = int(obj.get("heartbeat_interval") or 30)
    except (TypeError, ValueError):
        interval = 30
    return AgentConfig(server_url=server_url, agent_id=agent_id, heartbeat_interval=interval)


def save_config(config: AgentConfig) -> None:
    """原子写 config.json。"""
    obj = {
        "server_url": config.server_url,
        "agent_id": config.agent_id,
        "heartbeat_interval": config.heartbeat_interval,
    }
    _atomic_write(
        paths.CONFIG_FILE, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    )


# ---------------------------------------------------------------- 凭证存取


def save_token(token: str) -> None:
    """DPAPI LOCAL_MACHINE 加密写入 credential.bin。"""
    _atomic_write(paths.CREDENTIAL_FILE, _dpapi_protect(token.encode("utf-8")))


def load_token() -> str | None:
    """读取并解密 credential.bin；不存在/解密失败返回 None。"""
    try:
        cipher = paths.CREDENTIAL_FILE.read_bytes()
        return _dpapi_unprotect(cipher).decode("utf-8")
    except (OSError, RuntimeError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------- bind


def bind(server_url: str, token: str, agent_id: str | None = None) -> AgentConfig:
    """``sau bind`` 真实实现：写 config.json（含 agent_id）+ credential.bin（DPAPI）。

    - ``agent_id`` 缺省时：沿用现有 config.json 中的值；仍缺失则生成 ``uuid4().hex``（32 位）；
    - 重绑定时沿用既有 ``heartbeat_interval`` 自定义值（review 修复）；
    - 返回落盘后的配置（供 CLI 回显）。
    """
    server_url = server_url.strip().rstrip("/")
    if not server_url.lower().startswith(("ws://", "wss://")):
        raise ValueError("server_url 必须以 ws:// 或 wss:// 开头")
    token = token.strip()
    if not token:
        raise ValueError("token 不能为空")

    existing = load_config()
    if not agent_id:
        agent_id = existing.agent_id if existing else uuid.uuid4().hex
    agent_id = agent_id.strip().lower()

    config = AgentConfig(
        server_url=server_url,
        agent_id=agent_id,
        heartbeat_interval=existing.heartbeat_interval if existing else 30,
    )
    save_config(config)
    save_token(token)
    return config
