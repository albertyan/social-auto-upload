"""
sau_tray.controllers.login_ctrl
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
平台登录控制器。

管理登录流程（Playwright 调用）和线程管理。
"""
from __future__ import annotations

import logging
import threading

from sau_tray.core import gui_thread
from sau_tray.core.config import PLATFORM_DISPLAY_NAMES
from sau_tray.views.login_view import LoginView
from sau_tray.views.dialogs import show_notify

logger = logging.getLogger(__name__)


class LoginController:
    """平台登录控制器。

    持有 LoginView 引用，处理登录业务逻辑。
    """

    def __init__(self) -> None:
        logger.info("LoginController 构造，创建 LoginView 实例")  # 为什么打这条日志：追踪 LoginController 初始化
        self._view = LoginView(on_login=self.login)

    @property
    def view(self) -> LoginView:
        """返回 View 引用。"""
        return self._view

    def login(self, platform_key: str) -> None:
        """启动平台登录流程（在后台线程中执行）。

        Parameters
        ----------
        platform_key : str
            平台标识（如 "douyin", "xiaohongshu" 等）。
        """
        display_name = PLATFORM_DISPLAY_NAMES.get(platform_key, platform_key)
        logger.info("登录流程开始: platform=%s (%s)", platform_key, display_name)  # 为什么打这条日志：追踪登录启动的平台，排查登录触发问题

        def _do_login() -> None:
            try:
                from sau_tray.login_flows import do_login
                do_login(platform_key)
                display_name_inner = PLATFORM_DISPLAY_NAMES.get(platform_key, platform_key)
                logger.info("登录流程完成: platform=%s (%s)", platform_key, display_name_inner)  # 为什么打这条日志：确认登录成功完成
                show_notify(f"{display_name_inner} 登录完成")
            except Exception as e:
                logger.error("登录流程失败: platform=%s, error=%s", platform_key, e)  # 为什么打这条日志：记录登录失败的平台和异常信息，排查登录失败原因
                show_notify(f"登录失败: {e}")

        t = threading.Thread(target=_do_login, daemon=True, name=f"SAU-Login-{platform_key}")
        logger.info("创建登录后台线程: name=%s", t.name)  # 为什么打这条日志：确认登录线程已创建
        gui_thread.add_login_thread(t)
        t.start()
