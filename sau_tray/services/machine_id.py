"""
sau_tray.services.machine_id
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
机器码获取服务（带缓存，线程安全）。

从 tray_app.py 提取。可独立调用，不依赖 tray_app.py。
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 机器码缓存（启动时计算一次，避免每次打开设置页面都调用 WMI/PowerShell）
# ---------------------------------------------------------------------------
_machine_code_cache: str | None = None
_machine_code_lock = threading.Lock()


def get_machine_code() -> str:
    """获取机器码（带缓存，线程安全）。

    首次调用时计算并缓存，后续直接返回缓存值。
    """
    global _machine_code_cache
    with _machine_code_lock:
        if _machine_code_cache is not None:
            return _machine_code_cache
        try:
            from sau_agent_pkg.machine import get_machine_code as _real_get
            _machine_code_cache = _real_get()
        except Exception as e:
            err_str = str(e).lower()
            # 采集失败分类：根据异常信息判断来源（guid / serial / cpu）
            if "guid" in err_str or "uuid" in err_str or "machineguid" in err_str:
                logger.error("机器码采集失败[guid类]: %s", e)  # 为什么打这条日志：区分机器码 GUID 采集失败类别
            elif "serial" in err_str or "bios" in err_str or "baseboard" in err_str:
                logger.error("机器码采集失败[serial类]: %s", e)  # 为什么打这条日志：区分 BIOS/序列号采集失败类别
            elif "cpu" in err_str or "processor" in err_str or "cpuid" in err_str:
                logger.error("机器码采集失败[cpu类]: %s", e)  # 为什么打这条日志：区分 CPU 信息采集失败类别
            else:
                logger.error("机器码采集失败[其他]: %s", e)  # 为什么打这条日志：记录其他未分类的采集失败
            _machine_code_cache = "N/A"
        return _machine_code_cache


def preload() -> None:
    """在应用启动时预加载机器码（后台线程调用，避免阻塞主流程）。"""
    logger.info("machine_id.preload: 开始后台预加载机器码")  # 为什么打这条日志：确认后台预加载线程已启动
    try:
        code = get_machine_code()
        prefix = str(code)[:8]
        logger.info("machine_id.preload: 机器码预加载完成，缓存前 8 位: %s", prefix)  # 为什么打这条日志：确认机器码预加载成功（仅记前缀，脱敏）
    except Exception as e:
        logger.error("machine_id.preload: 预加载异常: %s", e)  # 为什么打这条日志：记录预加载阶段的异常
        pass
