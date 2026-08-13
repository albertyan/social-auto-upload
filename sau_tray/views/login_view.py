"""
sau_tray.views.login_view
~~~~~~~~~~~~~~~~~~~~~~~~~~
平台登录视图。

从 tray_app.py 的 _make_login_callback() 中的 UI 部分提取。
通过回调与 Controller 通信。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from sau_tray.views.base import BaseView

logger = logging.getLogger(__name__)


class LoginView(BaseView):
    """平台登录窗口（轻量状态显示）。

    登录流程主要由 Playwright 驱动浏览器完成，
    此视图仅用于显示登录进度/结果通知。

    Parameters
    ----------
    on_login : callable
        签名 (platform_key) → None
        触发登录流程。
    """

    def __init__(self, on_login: Callable):
        super().__init__()
        logger.info("LoginView 初始化完成")  # 为什么打这条日志：追踪 LoginView 生命周期
        self._on_login = on_login
        self._platform_key: str | None = None

    def login(self, platform_key: str) -> None:
        """启动指定平台的登录流程。"""
        logger.info("LoginView.login: 打开登录流程，平台=%s", platform_key)  # 为什么打这条日志：记录 LoginView 登录触发点
        self._platform_key = platform_key
        self._on_login(platform_key)

    def show(self) -> None:
        """显示登录视图。"""
        logger.info("LoginView.show: 打开 LoginView 窗口")  # 为什么打这条日志：追踪 LoginView 打开时机
        super().show()

    def close(self) -> None:
        """关闭登录视图。"""
        logger.info("LoginView.close: 关闭 LoginView 窗口")  # 为什么打这条日志：追踪 LoginView 关闭时机
        super().close()

    def _create(self, root: Any) -> None:
        """创建登录状态窗口（在 GUI 线程中执行）。

        当前登录流程为 Playwright 驱动浏览器，无需独立窗口。
        登录结果通过系统通知 (show_notify) 反馈给用户。
        此方法保留供未来扩展（如需要显示登录进度窗口）。
        """
        logger.info("LoginView._create: GUI 线程创建窗口（当前为占位实现）")  # 为什么打这条日志：确认 _create 被 GUI 线程调用
        # 登录流程使用浏览器自动化，不需要额外的 tkinter 窗口。
        # 登录结果通过 views.dialogs.show_notify 通知用户。
        pass
