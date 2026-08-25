# -*- coding: utf-8 -*-
"""运行时数据布局（设计文档 §3.6，v1.2 定案）。

运行时数据统一落在 ``%ProgramData%\\SAU\\``（服务与托盘共享、跨用户会话可见）：

    %ProgramData%\\SAU\\
    ├── config.json          # 绑定与运行配置
    ├── credential.bin       # Agent 凭证（DPAPI，后续步骤实现）
    ├── local_token.bin      # 本地 API 访问令牌（后续步骤实现）
    ├── cookies\\             # 各平台账号 cookies
    ├── db\\                  # 本地 SQLite（WAL）
    ├── logs\\                # 日志（§14.1）
    ├── downloads\\           # 任务素材下载工作目录
    ├── browsers\\            # 浏览器内核
    └── updates\\             # 升级编排工作区

本模块仅提供路径解析与目录初始化能力，本步（任务 #12 第一步）只用到
``logs/``；其余目录在对应后续步骤实现时按需创建。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 运行时数据根目录（%ProgramData%\SAU）；SAU_DATA_ROOT 环境变量可覆盖（测试隔离）
DATA_ROOT: Path = Path(
    os.environ.get("SAU_DATA_ROOT")
    or (Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "SAU")
)

#: 日志目录（§3.6 / §14.1）
LOGS_DIR: Path = DATA_ROOT / "logs"

#: 本地库目录
DB_DIR: Path = DATA_ROOT / "db"

#: cookies 目录
COOKIES_DIR: Path = DATA_ROOT / "cookies"

#: 任务素材下载目录
DOWNLOADS_DIR: Path = DATA_ROOT / "downloads"

#: 浏览器内核目录
BROWSERS_DIR: Path = DATA_ROOT / "browsers"

#: 升级编排工作区
UPDATES_DIR: Path = DATA_ROOT / "updates"

#: 服务日志文件（§14.1：10MB × 5 轮转）
SERVICE_LOG_FILE: Path = LOGS_DIR / "service.log"

#: 托盘日志文件（§14.1：5MB × 3 轮转）
TRAY_LOG_FILE: Path = LOGS_DIR / "tray.log"

#: 升级日志文件（§14.1：5MB × 3 轮转）
UPGRADE_LOG_FILE: Path = LOGS_DIR / "upgrade.log"


#: 配置文件（§3.6：绑定与运行配置）
CONFIG_FILE: Path = DATA_ROOT / "config.json"

#: Agent 凭证文件（DPAPI LOCAL_MACHINE 加密，§5.8）
CREDENTIAL_FILE: Path = DATA_ROOT / "credential.bin"

#: 本地 API 访问令牌（§3.6：服务每次启动重新生成，users 可读，托盘/CLI 持其调 5409）
LOCAL_TOKEN_FILE: Path = DATA_ROOT / "local_token.bin"

#: 本地 SQLite 数据库文件（WAL，§5.5）
DB_FILE: Path = DB_DIR / "sau.db"


def ensure_dir(path: Path) -> Path:
    """确保目录存在（不存在则递归创建），返回该目录。"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_logs_dir() -> Path:
    """确保日志目录存在并返回（服务/托盘/CLI 启动时调用）。"""
    return ensure_dir(LOGS_DIR)


def ensure_db_dir() -> Path:
    """确保本地库目录存在并返回。"""
    return ensure_dir(DB_DIR)
