"""
sau_agent_pkg.config
~~~~~~~~~~~~~~~~~~~~
SAU Agent 配置管理模块。

职责：
- SAU_HOME 路径解析（%ProgramData%\\SAU 或环境变量覆盖）
- config.json 读写（server_url、agent_id、心跳间隔、max_concurrency 等）
- DPAPI 加密/解密 credential.bin（agent_token，机器级保护）
- local_token.bin 管理（本地控制 API 随机令牌）
"""
from __future__ import annotations

import json
import os
import secrets
import uuid
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# SAU_HOME 路径解析
# ---------------------------------------------------------------------------
# 优先使用环境变量 SAU_HOME，否则使用 %ProgramData%\SAU
SAU_HOME: Path = Path(os.environ.get("SAU_HOME") or os.environ.get("ProgramData", ".")) / "SAU"


def ensure_sau_home() -> None:
    """确保 SAU_HOME 及子目录存在。"""
    for sub in ("cookies", "downloads", "logs", "logs/tasks", "db"):
        (SAU_HOME / sub).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# config.json 读写
# ---------------------------------------------------------------------------
_CONFIG_FILENAME = "config.json"

# 默认配置
_DEFAULT_CONFIG: dict[str, Any] = {
    "server_url": "wss://localhost/opcgeo/agent/ws",
    "agent_id": "",
    "heartbeat_interval": 30,       # 秒
    "max_concurrency": 2,           # 最大并发上传任务数
    "account_check_interval": 3600, # 账号有效性检查间隔（秒）
    "reconnect_backoff_max": 300,   # 重连退避上限（秒）
}


def _config_path() -> Path:
    return SAU_HOME / _CONFIG_FILENAME


def load_config() -> dict[str, Any]:
    """
    加载 config.json，若不存在则返回默认配置。
    缺失的字段会用默认值补齐。
    """
    path = _config_path()
    if not path.exists():
        return dict(_DEFAULT_CONFIG)
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # 补齐缺失的默认字段
        merged = dict(_DEFAULT_CONFIG)
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULT_CONFIG)


def save_config(cfg: dict[str, Any]) -> None:
    """将配置写入 config.json。"""
    SAU_HOME.mkdir(parents=True, exist_ok=True)
    path = _config_path()
    with path.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# agent_id 管理
# ---------------------------------------------------------------------------
def generate_agent_id() -> str:
    """生成 UUID-based agent_id 并持久化到 config.json，返回生成的 id。"""
    agent_id = uuid.uuid4().hex
    cfg = load_config()
    cfg["agent_id"] = agent_id
    save_config(cfg)
    return agent_id


def get_agent_id() -> str:
    """返回当前 agent_id，若为空则自动生成并持久化。"""
    cfg = load_config()
    agent_id = cfg.get("agent_id", "")
    if not agent_id:
        agent_id = generate_agent_id()
    return agent_id


# ---------------------------------------------------------------------------
# DPAPI 加密/解密 credential.bin（agent_token）
# ---------------------------------------------------------------------------
_CREDENTIAL_FILENAME = "credential.bin"
_DPAPI_DESC = "sau"


def _credential_path() -> Path:
    return SAU_HOME / _CREDENTIAL_FILENAME


def save_token(plain: str) -> None:
    """
    使用 DPAPI 加密并保存 agent_token（机器级保护）。
    服务进程以 SYSTEM 身份写入，托盘进程（安装用户）及 SERVICE 均可解密。
    """
    import win32crypt  # type: ignore[import-untyped]

    blob = win32crypt.CryptProtectData(
        plain.encode("utf-8"),
        _DPAPI_DESC,       # szDataDescr
        None,              # pOptionalEntropy
        None,              # pvReserved
        None,              # pPromptStruct
        win32crypt.CRYPTPROTECT_LOCAL_MACHINE,
    )
    SAU_HOME.mkdir(parents=True, exist_ok=True)
    _credential_path().write_bytes(blob)


def load_token() -> str | None:
    """
    解密并返回 agent_token。
    若文件不存在或解密失败（换机/密文损坏）返回 None。
    """
    import win32crypt  # type: ignore[import-untyped]

    path = _credential_path()
    if not path.exists():
        return None
    try:
        _, raw = win32crypt.CryptUnprotectData(
            path.read_bytes(),
            None,   # ppszDataDescr
            None,   # pOptionalEntropy
            None,   # pvReserved
            0,      # dwFlags
        )
        return raw.decode("utf-8")
    except Exception:
        return None


def delete_token() -> None:
    """删除 credential.bin（解绑本机时使用）。"""
    path = _credential_path()
    if path.exists():
        path.unlink()


# ---------------------------------------------------------------------------
# local_token.bin 管理（本地控制 API 随机令牌）
# ---------------------------------------------------------------------------
_LOCAL_TOKEN_FILENAME = "local_token.bin"


def _local_token_path() -> Path:
    return SAU_HOME / _LOCAL_TOKEN_FILENAME


def generate_local_token() -> str:
    """生成并保存本地控制 API 随机令牌（服务启动时调用）。"""
    token = secrets.token_urlsafe(32)
    SAU_HOME.mkdir(parents=True, exist_ok=True)
    _local_token_path().write_text(token, encoding="utf-8")
    return token


def load_local_token() -> str | None:
    """读取本地控制 API 令牌，不存在返回 None。"""
    path = _local_token_path()
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
