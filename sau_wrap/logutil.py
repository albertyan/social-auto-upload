# -*- coding: utf-8 -*-
"""日志初始化（设计文档 §14.1 日志布局）。

| 文件        | 来源                       | 轮转策略   |
| ----------- | -------------------------- | ---------- |
| service.log | 服务进程（sau.exe agent）  | 10MB × 5   |
| tray.log    | 托盘进程                   | 5MB × 3    |
| upgrade.log | 升级编排（含 runner 副本） | 5MB × 3    |

日志脱敏规则（凭证 / cookie / 手机号等）在后续 Agent 核心步骤落实（§3.8 第 3 条）。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from sau_wrap import paths

_MB = 1024 * 1024
_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

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

    file_handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if also_console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    logger.propagate = False
    _CONFIGURED.add(name)
    return logger


def get_service_logger(also_console: bool = False) -> logging.Logger:
    """服务进程日志（%ProgramData%\\SAU\\logs\\service.log）。"""
    return setup_logger("sau.service", paths.SERVICE_LOG_FILE, also_console=also_console)
