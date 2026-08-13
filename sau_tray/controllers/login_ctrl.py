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
        def _do_login() -> None:
            try:
                from sau_tray.login_flows import do_login
                do_login(platform_key)
                display_name = PLATFORM_DISPLAY_NAMES.get(platform_key, platform_key)
                show_notify(f"{display_name} 登录完成")
            except Exception as e:
                show_notify(f"登录失败: {e}")

        t = threading.Thread(target=_do_login, daemon=True, name=f"SAU-Login-{platform_key}")
        gui_thread.add_login_thread(t)
        t.start()
