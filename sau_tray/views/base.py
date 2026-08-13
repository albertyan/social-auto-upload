"""
sau_tray.views.base
~~~~~~~~~~~~~~~~~~~
View 基类，封装 GUI 线程调度。
子类只需关注 UI 构建，无需关心线程安全问题。
"""
from __future__ import annotations

from sau_tray.core import gui_thread


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
        gui_thread.schedule(self._create)

    def close(self):
        """通过 GUI 线程队列调度窗口销毁。"""
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
                    self._window.destroy()
            except Exception:
                pass
            self._window = None
