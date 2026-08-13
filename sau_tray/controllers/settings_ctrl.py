"""
sau_tray.controllers.settings_ctrl
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
设置窗口控制器。

处理测试连接（HTTP 请求在后台线程执行）和保存配置。
通过 gui_thread.schedule 回传结果更新 UI。
"""
from __future__ import annotations

import logging
import threading
import urllib.error
import urllib.request
from typing import Any

from sau_tray.core import gui_thread
from sau_tray.views.settings_view import SettingsView
from sau_tray.views.dialogs import show_notify

logger = logging.getLogger(__name__)


class SettingsController:
    """设置窗口控制器。

    持有 SettingsView 引用，处理业务逻辑。
    View 通过回调与 Controller 通信，Controller 通过 View 的公开方法更新 UI。
    """

    def __init__(self) -> None:
        self._view = SettingsView(
            on_test_connection=self.test_connection,
            on_save_config=self.save_config,
        )

    @property
    def view(self) -> SettingsView:
        """返回 View 引用（供 tray_app 调用 show/close）。"""
        return self._view

    # ------------------------------------------------------------------
    # 测试连接
    # ------------------------------------------------------------------
    def test_connection(
        self,
        server_url: str,
        token: str,
        status_callback: Any,
    ) -> None:
        """测试服务器连接（HTTP 请求在后台线程执行）。

        Parameters
        ----------
        server_url : str
            WebSocket 服务器地址（ws:// 或 wss://）。
        token : str
            Agent Token。
        status_callback : callable
            签名 (text: str) → None，用于更新 UI 状态文本。
            在 GUI 线程中调用。
        """
        # 将 ws(s) 转为 http(s) 做 HTTP 探测（保留完整路径，探测 WS 端点本身）
        probe_url = server_url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)

        def _do_test() -> None:
            try:
                req = urllib.request.Request(probe_url, method="GET")
                req.add_header("User-Agent", "SAU-Agent/1.0")
                req.add_header("Authorization", f"Bearer {token}")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    code = resp.getcode()
                    if 200 <= code < 300:
                        result_text = f"连接成功 (服务器可达 HTTP {code})"
                    else:
                        result_text = f"连接成功 (HTTP {code})"
            except urllib.error.HTTPError as e:
                if e.code in (400, 405, 426):
                    # 普通 HTTP 请求打到真 WebSocket 端点的预期响应
                    result_text = f"连接成功 (WS 端点可达 HTTP {e.code})"
                elif e.code in (401, 403):
                    result_text = f"服务器可达，但认证未通过 (HTTP {e.code})，请检查 Token"
                elif e.code == 404:
                    result_text = (
                        f"服务器可达但路径不存在 (HTTP 404)，"
                        "请检查地址是否包含上下文路径（如 /opcgeo）"
                    )
                elif e.code < 500:
                    result_text = f"服务器可达 (HTTP {e.code})"
                else:
                    result_text = f"服务器异常 (HTTP {e.code})"
            except urllib.error.URLError as e:
                result_text = f"无法连接: {e.reason}"
            except Exception as e:
                result_text = f"连接失败: {e}"
            # 通过 GUI 线程调度更新 UI
            gui_thread.schedule(lambda root: status_callback(result_text))

        threading.Thread(target=_do_test, daemon=True).start()

    # ------------------------------------------------------------------
    # 保存配置
    # ------------------------------------------------------------------
    def save_config(self, server_url: str, token: str) -> None:
        """保存设置到配置文件。"""
        from sau_agent_pkg.config import load_config, save_config

        if server_url and not (server_url.startswith("ws://") or server_url.startswith("wss://")):
            def _show_warn() -> None:
                from tkinter import messagebox
                messagebox.showwarning(
                    "格式错误", "服务器地址应以 ws:// 或 wss:// 开头",
                    parent=self._view.window,
                )
            gui_thread.schedule(lambda root: _show_warn())
            return

        cfg_new = load_config()
        if server_url:
            cfg_new["server_url"] = server_url
        if token:
            cfg_new["token"] = token
        save_config(cfg_new)

        show_notify("设置已保存", "SAU 设置")
        self._view.close()
