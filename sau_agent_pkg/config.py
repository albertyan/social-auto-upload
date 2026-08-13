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
import logging
import os
import secrets
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SAU_HOME 路径解析
# ---------------------------------------------------------------------------
# 优先使用环境变量 SAU_HOME，否则使用 %ProgramData%\SAU
SAU_HOME: Path = Path(os.environ.get("SAU_HOME") or os.environ.get("ProgramData", ".")) / "SAU"


def ensure_sau_home() -> None:
    """确保 SAU_HOME 及子目录存在。"""
    for sub in ("cookies", "downloads", "logs", "logs/tasks", "db"):
        try:
            (SAU_HOME / sub).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # 目录创建失败告警：后续所有写操作（config/db/log）都会失败，必须让运维知道是哪一级出了问题
            logger.warning("ensure_sau_home: failed to create dir %s, error: %s", SAU_HOME / sub, e)


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
        # 找不到文件用默认：首次安装/误删后能自动恢复，info 让运维知道当前配置来源不是磁盘
        logger.info("load_config: %s not found, using DEFAULT_CONFIG (%d fields)", path, len(_DEFAULT_CONFIG))
        return dict(_DEFAULT_CONFIG)
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # 补齐缺失的默认字段
        merged = dict(_DEFAULT_CONFIG)
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError) as e:
        # 解析失败用默认：文件损坏/被截断/权限不足时不至于让 agent 起不来，info 标注来源与错误类型
        logger.info("load_config: parse %s failed (%s), using DEFAULT_CONFIG (%d fields)", path, type(e).__name__, len(_DEFAULT_CONFIG))
        return dict(_DEFAULT_CONFIG)


def save_config(cfg: dict[str, Any]) -> None:
    """将配置写入 config.json。"""
    field_count = len(cfg)
    # 写入前 info 摘要：记录即将写入多少字段，便于后续排查"配置没写进去"时对比数量
    logger.info("save_config: about to write %d fields to %s", field_count, _config_path())
    SAU_HOME.mkdir(parents=True, exist_ok=True)
    path = _config_path()
    with path.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    # 写入后 info 摘要：确认写操作走完流程，字段数与写入前一致则说明没中途截断
    logger.info("save_config: wrote %d fields to %s successfully", field_count, path)


# ---------------------------------------------------------------------------
# agent_id 管理
# ---------------------------------------------------------------------------
def generate_agent_id() -> str:
    """生成 UUID-based agent_id 并持久化到 config.json，返回生成的 id。"""
    agent_id = uuid.uuid4().hex
    # auto-generate 时 info 记录前 8 位：既方便日志里 trace 身份，又不会把完整 32 位 UUID 打满日志
    logger.info("generate_agent_id: auto-generated agent_id prefix=%s", agent_id[:8])
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

# 为什么硬编码 0x04，而不使用 win32con/win32crypt 的常量名：
# pywin32 不同版本暴露的 CRYPTPROTECT_LOCAL_MACHINE 位置不一致，
# 有的版本在 win32con 下，有的在 win32crypt 下，有的两者都没有（仅文档值）。
# Microsoft 公开文档中该 flag 的值恒为 0x04，直接写值可免除 import 与版本兼容负担。
_CRYPTPROTECT_LOCAL_MACHINE_FLAG = 0x04


def _credential_path() -> Path:
    return SAU_HOME / _CREDENTIAL_FILENAME


def save_token(plain: str) -> None:
    """
    使用 DPAPI 加密并保存 agent_token（机器级保护）。
    服务进程以 SYSTEM 身份写入，托盘进程（安装用户）及 SERVICE 均可解密。
    """
    # 写入 info，仅记长度不记明文：确认"绑定 token"操作确实落到磁盘，同时避免敏感信息泄露到日志
    logger.info("save_token: about to write token, length=%d", len(plain))
    import win32crypt  # type: ignore[import-untyped]

    blob = win32crypt.CryptProtectData(
        plain.encode("utf-8"),
        _DPAPI_DESC,       # szDataDescr
        None,              # pOptionalEntropy
        None,              # pvReserved
        None,              # pPromptStruct
        _CRYPTPROTECT_LOCAL_MACHINE_FLAG,
    )
    SAU_HOME.mkdir(parents=True, exist_ok=True)
    _credential_path().write_bytes(blob)
    logger.info("save_token: token written successfully, length=%d", len(plain))


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
    except Exception as e:
        # 解密失败 warning：典型场景是换机（MachineGuid 变了 DPAPI 解不开）或文件损坏，
        # 打 warning 让运维从日志一眼看出是 credential 问题，而不是网络/服务端鉴权问题
        logger.warning("load_token: decrypt failed (%s), likely machine changed or credential corrupted", type(e).__name__)
        return None


def delete_token() -> None:
    """删除 credential.bin（解绑本机时使用）。"""
    path = _credential_path()
    if path.exists():
        # 删除 info：解绑操作要有审计痕迹，便于事后排查"什么时候 token 被清了"
        logger.info("delete_token: removing %s", path)
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
    try:
        SAU_HOME.mkdir(parents=True, exist_ok=True)
        _local_token_path().write_text(token, encoding="utf-8")
    except OSError as e:
        # create 失败 warning：后续托盘和服务之间的本地 API 调用会全部 401，必须告警
        logger.warning("generate_local_token: write %s failed: %s", _local_token_path(), e)
        raise
    # 仅记录长度：确认令牌写成功，又不把明文 token 打入日志
    logger.info("generate_local_token: created successfully, length=%d", len(token))
    return token


def load_local_token() -> str | None:
    """读取本地控制 API 令牌，不存在返回 None。"""
    path = _local_token_path()
    exists = path.exists()
    # 是否存在：托盘连不上服务时可以通过这条日志快速判断是 token 没生成还是网络/端口问题
    # logger.debug("load_local_token: token file exists=%s", exists)
    if not exists:
        return None
    try:
        token = path.read_text(encoding="utf-8").strip()
        # 仅记录长度：核对令牌长度是否合理（太短意味着文件可能被截断）
        # logger.debug("load_local_token: loaded successfully, length=%d", len(token))
        return token
    except OSError as e:
        # open 失败 warning：文件存在但读不出，多半是权限/磁盘坏道，需要告警
        logger.error("load_local_token: read %s failed: %s", path, e)
        return None
