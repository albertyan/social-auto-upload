# -*- coding: utf-8 -*-
"""日志初始化（设计文档 §14.1 日志布局）。

| 文件        | 来源                       | 轮转策略   |
| ----------- | -------------------------- | ---------- |
| service.log | 服务进程（sau.exe agent）  | 10MB × 5   |
| tray.log    | 托盘进程                   | 5MB × 3    |
| upgrade.log | 升级编排（含 runner 副本） | 5MB × 3    |

日志脱敏（§3.8 第 3 条，终审修复⑦）：本模块 :class:`SanitizeFilter` 已接入全部
轮转/控制台句柄，覆盖 service / tray / upgrade 三条日志通道与 [AUDIT] 行：
token/cookie/password/secret/Bearer 等键值只记长度掩码、手机号中段掩码、
素材签名 URL 去 query。
"""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from sau_wrap import paths

_MB = 1024 * 1024
_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# ---------------------------------------------------------------- 脱敏（§3.8 第 3 条）

#: 凭证类键值（key=value / key: value）：值只记长度，不记内容。
#: 覆盖 token / access_token / cookie / password / secret / authorization /
#: credential / api_key / X-SAU-Local-Token 等（终审修复⑦）。
_MASK_KEY_RE = re.compile(
    r"(?i)\b(access[_-]?token|x-sau-local-token|authorization|token|cookies?|"
    r"password|passwd|secret|credential|api[_-]?key)"
    r"(\s*[:=]\s*)"
    # 值前瞻排除 Bearer：``Authorization: Bearer <tok>`` 由 _BEARER_RE 先行整体掩码，
    # 避免键值规则只掩掉 "Bearer" 而泄露后续真令牌。
    r"(['\"]?)(?!Bearer\b)(\S+?)\3(?=[\s,;)\]}'\"]|$)")

#: ``Bearer <token>`` 形态（WS 连接头/HTTP 头）
_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._\-~+/=]{8,}")

#: 手机号：前 3 后 4 保留，中段 4 位掩码（138****1234）
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")

#: 素材签名 URL：query 含签名/凭证类参数时整个 query 掩码（防签名泄露可重放）
_SIGNED_URL_RE = re.compile(
    r"(?i)(https?://[^\s'\"<>]+?)\?[^\s'\"<>]*?"
    r"(signature|x-amz|x-oss-sign|accesskey|expires|token|secret)[^\s'\"<>]*")


def mask_sensitive(text: str) -> str:
    """按 §3.8 第 3 条规则脱敏单条日志文本（幂等，无敏感内容原样返回）。

    规则顺序：签名 URL 去 query → Bearer 整体掩码 → 凭证键值掩码 → 手机号；
    Bearer 先于键值规则，保证 ``Authorization: Bearer <tok>`` 真令牌不残留。
    """
    text = _SIGNED_URL_RE.sub(lambda m: m.group(1) + "?<masked>", text)
    text = _BEARER_RE.sub(r"\1***", text)
    text = _MASK_KEY_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}***len={len(m.group(4))}{m.group(3)}",
        text)
    text = _PHONE_RE.sub(r"\1****\2", text)
    return text


class SanitizeFilter(logging.Filter):
    """日志脱敏过滤器（终审修复⑦）：先格式化再脱敏，回写 msg 并清空 args。

    挂在句柄上（而非 logger）：对经由该句柄的**所有**记录生效，含子 logger
    传递上来的记录与 [AUDIT] 行；脱敏自身异常不影响日志主链路。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = mask_sensitive(str(record.getMessage()))
            record.args = None
        except Exception:  # pragma: no cover - 兜底：脱敏失败不阻断日志
            pass
        return True

#: §14.1 轮转策略：文件 → (单文件大小, 保留份数)
_ROTATION = {
    "service.log": (10 * _MB, 5),
    "tray.log": (5 * _MB, 3),
    "upgrade.log": (5 * _MB, 3),
}

_CONFIGURED: set[str] = set()


def setup_logger(
    name: str,
    log_file: Path,
    *,
    level: int = logging.INFO,
    also_console: bool = False,
) -> logging.Logger:
    """初始化并返回带轮转文件输出的 logger（幂等，重复调用不叠加句柄）。"""
    logger = logging.getLogger(name)
    if name in _CONFIGURED:
        return logger

    paths.ensure_logs_dir()
    max_bytes, backup_count = _ROTATION.get(log_file.name, (5 * _MB, 3))

    logger.setLevel(level)
    formatter = logging.Formatter(_FMT)
    sanitize = SanitizeFilter()  # 终审修复⑦：全部句柄统一脱敏（§3.8 第 3 条）

    file_handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(sanitize)
    logger.addHandler(file_handler)

    if also_console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(formatter)
        console_handler.addFilter(sanitize)
        logger.addHandler(console_handler)

    logger.propagate = False
    _CONFIGURED.add(name)
    return logger


def get_service_logger(also_console: bool = False) -> logging.Logger:
    """服务进程日志（%ProgramData%\\SAU\\logs\\service.log）。"""
    return setup_logger("sau.service", paths.SERVICE_LOG_FILE, also_console=also_console)
