"""
sau_tray.views.base
~~~~~~~~~~~~~~~~~~~
View 基类，封装 GUI 线程调度。
子类只需关注 UI 构建，无需关心线程安全问题。
"""
from __future__ import annotations

import logging

from sau_tray.core import gui_thread

logger = logging.getLogger(__name__)


class BaseView:
    """封装 GUI 线程调度，子类只需关注 UI 构建。"""

    def __init__(self):
        self._window = None

    @property
    def window(self):
        """返回当前窗口引用（可能为 None）。"""
        return self._window

    def show(self):
        """通过 GUI 线程队列调度窗口创建。"""
        logger.info("BaseView.show: 调度窗口显示 (class=%s)", self.__class__.__name__)  # 为什么打这条日志：追踪窗口显示请求，排查 UI 不显示问题
        gui_thread.schedule(self._create)

    def close(self):
        """通过 GUI 线程队列调度窗口销毁。"""
        logger.info("BaseView.close: 调度窗口关闭 (class=%s)", self.__class__.__name__)  # 为什么打这条日志：追踪窗口关闭请求，排查 UI 残留问题
        gui_thread.schedule(lambda root: self._destroy())

    def _create(self, root):
        """子类实现：创建窗口和控件（在 GUI 线程中执行）。

        Parameters
        ----------
        root : tkinter.Tk
            GUI 线程中的根窗口（由 gui_thread 传入）。
        """
        raise NotImplementedError

    def _destroy(self):
        """销毁窗口。"""
        if self._window is not None:
            try:
                if self._window.winfo_exists():
                    logger.info("BaseView._destroy: 销毁窗口 (class=%s)", self.__class__.__name__)  # 为什么打这条日志：确认窗口实际已销毁
                    self._window.destroy()
            except Exception:
                pass
            self._window = None
