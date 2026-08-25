# -*- coding: utf-8 -*-
"""Agent 核心骨架（任务 #12 第一步：空跑原型）。

本步行为（验收要求）：
- 打印启动日志（版本号 / PID / 数据目录）；
- 保持运行，周期性记录存活心跳；
- 响应停止信号，打印停止日志后退出。

【后续步骤实现】（实施计划 S2）：
- 建立到 opcgeo 的 WS 主循环（重连退避 2s→300s、凭证类关闭码挂起零重连）；
- 任务调度与回执上报（result_queue 断线补发）；
- 5409 本地 API 由 service/server.py 承载（S3）。
"""

from __future__ import annotations

import logging
import os

import win32event

from sau_wrap.version import APP_VERSION

#: 心跳日志间隔（秒）——骨架阶段用于验证服务持续运行
HEARTBEAT_SECONDS = 10
#: 停止信号轮询粒度（毫秒）
_POLL_MS = 500


class AgentSkeleton:
    """空跑 Agent：仅日志 + 等待停止，不建立任何网络连接。"""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def run(self, stop_event_handle: int) -> None:
        """阻塞运行，直到 ``stop_event_handle`` 被置位（服务停止）。"""
        self._logger.info(
            "agent 骨架启动: version=%s pid=%s (本步为空跑原型，不连接 WS)",
            APP_VERSION,
            os.getpid(),
        )
        elapsed_seconds = 0
        while True:
            rc = win32event.WaitForSingleObject(stop_event_handle, _POLL_MS)
            if rc == win32event.WAIT_OBJECT_0:
                break
            elapsed_seconds += _POLL_MS / 1000
            if elapsed_seconds % HEARTBEAT_SECONDS == 0:
                self._logger.debug("agent 骨架存活: 已运行 %.0f 秒", elapsed_seconds)
        self._logger.info("agent 骨架收到停止信号，退出主循环")
