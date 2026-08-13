"""
sau_tray.home_shim
~~~~~~~~~~~~~~~~~~
SAU_HOME 运行时垫片。

在 import sau_cli 之前调用 apply_home_shim()，将 conf.BASE_DIR 改写为
%ProgramData%\SAU（或环境变量 SAU_HOME 指定的路径），使所有上游函数的
cookie / 下载 / 日志路径统一指向安装数据目录。

只改内存属性，不落盘、不改源文件；上游升级后垫片自动继续生效。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SAU_HOME 路径计算（与 sau_agent_pkg/config.py 保持一致）
# ---------------------------------------------------------------------------
SAU_HOME: Path = Path(
    os.environ.get("SAU_HOME") or os.environ.get("ProgramData", ".")
) / "SAU"

# 标记：是否已经应用过垫片（防止重复执行）
_applied = False


def apply_home_shim() -> None:
    """
    必须在 import sau_cli 之前调用。

    行为：
    1. import conf（上游模块，保持原样）
    2. 改写 conf.BASE_DIR = SAU_HOME
    3. 创建 SAU_HOME 下所有必需子目录

    只改内存属性，不落盘、不改源文件。
    """
    global _applied
    if _applied:
        return

    # 1. 改写 conf.BASE_DIR
    try:
        import conf  # type: ignore[import-untyped]  # 上游模块
        conf.BASE_DIR = SAU_HOME
        logger.debug("conf.BASE_DIR → %s", SAU_HOME)
    except ImportError:
        # conf.py 不存在时（极早期环境）只创建目录，不中断
        logger.warning("conf module not found; home_shim skipped BASE_DIR rewrite")

    # 2. 创建所有必需子目录
    for sub in ("cookies", "downloads", "logs", "db", "logs/tasks"):
        try:
            (SAU_HOME / sub).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Cannot create directory %s: %s", SAU_HOME / sub, e)

    _applied = True
    logger.info("home_shim applied: SAU_HOME = %s", SAU_HOME)


def is_applied() -> bool:
    """返回垫片是否已应用。"""
    return _applied
