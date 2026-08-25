# -*- coding: utf-8 -*-
"""Agent 核心共享组件（原任务 #12 空跑骨架已退役，整合进 :mod:`ws_client`）。

保留本模块承载与传输层解耦的共享逻辑：

- :class:`ClockTracker`：服务端时钟偏差估计（现状文档 §3.8 可靠性语义 #1）——
  ``heartbeat_ack.server_time``（毫秒）与本机时间的差值做**滑动窗口 10 样本平均**，
  偏差绝对值 > 5 分钟 → 置调度暂停标志（本步仅标志位，真实调度后续步骤接入）。
"""

from __future__ import annotations

import time
from collections import deque

#: 滑动窗口样本数（§3.8 #1：10 样本平均）
CLOCK_WINDOW_SIZE = 10

#: 时钟偏差阈值（秒）：超过则暂停任务调度（§3.8 #1：5 分钟）
CLOCK_DRIFT_PAUSE_SECONDS = 300.0


class ClockTracker:
    """时钟偏差跟踪器：滑动窗口平均估计客户端与服务端的时钟偏差。"""

    def __init__(self, window_size: int = CLOCK_WINDOW_SIZE) -> None:
        self._samples: deque[float] = deque(maxlen=window_size)
        #: 偏差超阈值时为 True（本步仅标志位，供后续调度层消费）
        self.scheduling_paused = False

    def update(self, server_time_ms: int | float) -> float:
        """喂入一个 ``heartbeat_ack.server_time``（毫秒），返回当前平均偏差（秒）。

        偏差定义：``服务端时间 - 本机时间``（正数表示本机时钟偏慢）。
        """
        offset = float(server_time_ms) / 1000.0 - time.time()
        self._samples.append(offset)
        avg = sum(self._samples) / len(self._samples)
        self.scheduling_paused = abs(avg) > CLOCK_DRIFT_PAUSE_SECONDS
        return avg

    @property
    def offset_seconds(self) -> float:
        """当前平均偏差（秒）；无样本时 0。心跳 ``clock_offset_seconds`` 字段取值。"""
        if not self._samples:
            return 0.0
        return sum(self._samples) / len(self._samples)

    @property
    def sample_count(self) -> int:
        return len(self._samples)
