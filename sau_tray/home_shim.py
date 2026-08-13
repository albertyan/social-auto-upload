r"""
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
    before_base_dir = None
    after_base_dir = None
    try:
        import conf  # type: ignore[import-untyped]  # 上游模块
        before_base_dir = getattr(conf, "BASE_DIR", None)
        conf.BASE_DIR = SAU_HOME
        after_base_dir = conf.BASE_DIR
        logger.info("apply_home_shim: SAU_HOME 重写 conf.BASE_DIR，之前=%s，之后=%s",  # 为什么打这条日志：入口记录 BASE_DIR 改写前后对比，确认路径垫片生效
                    before_base_dir, after_base_dir)
    except ImportError:
        # conf.py 不存在时（极早期环境）只创建目录，不中断
        logger.warning("conf module not found; home_shim skipped BASE_DIR rewrite")

    # 2. 创建所有必需子目录
    for sub in ("cookies", "downloads", "logs", "db", "logs/tasks"):
        try:
            target = SAU_HOME / sub
            if not target.exists():
                logger.info("apply_home_shim: 子目录不存在，创建目录: %s", target)  # 为什么打这条日志：记录首次创建的子目录（不存在时才记）
            target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Cannot create directory %s: %s", SAU_HOME / sub, e)

    _applied = True
    logger.info("apply_home_shim: 垫片应用完成，SAU_HOME=%s", SAU_HOME)  # 为什么打这条日志：入口 info，确认垫片已应用


def is_applied() -> bool:
    """返回垫片是否已应用。"""
    return _applied
