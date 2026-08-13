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
        """保存设置到配置文件。

        为什么同时写 config.json 与 credential.bin：
        - SauAgentCore 读取 token 走 load_token()（读 credential.bin，DPAPI 加密机器级保护）
        - SettingsView 显示「当前已配置的 token」读 config.json / load_token 双路
        两边必须同步写入，否则保存后服务仍会报 No token bound。
        """
        from sau_agent_pkg.config import load_config, save_config, save_token, delete_token, get_agent_id

        token_len = len(token)
        logger.info("save_config 开始: server_url=%s, token_len=%d", server_url, token_len)  # 为什么打这条日志：记录保存参数（token 仅记长度，脱敏），排查配置保存问题

        if server_url and not (server_url.startswith("ws://") or server_url.startswith("wss://")):
            logger.warning("save_config: 服务器地址格式错误，非 ws(s):// 开头: %s", server_url)  # 为什么打这条日志：记录地址格式校验失败，排查配置错误
            # 为什么用 show_info 而不是 tkinter.messagebox.showwarning(parent=self._view.window)：
            # 1. save_config 可能从非 GUI 线程调用（虽然 Settings 保存按钮当前在 GUI 线程），
            #    show_info 内部自动切 GUI 线程，不受调用方线程影响。
            # 2. show_info 自动屏幕居中，tk 原生 messagebox 默认偏右下角，视觉不一致。
            # 3. 样式统一，所有"警告/信息型"弹框统一走 show_info。
            from sau_tray.views.dialogs import show_info
            logger.info("save_config: 弹窗提示格式错误（show_info）")  # 为什么打这条日志：确认格式错误弹窗已触发
            threading.Thread(
                target=show_info,
                args=("服务器地址应以 ws:// 或 wss:// 开头", "格式错误"),
                daemon=True, name="SAU-FormatWarning",
            ).start()
            return

        cfg_new = load_config()
        changed = False
        if server_url:
            cfg_new["server_url"] = server_url
            changed = True
        if token:
            # 1) 同步写 DPAPI 加密的 credential.bin（SauAgentCore 会读这里）
            try:
                save_token(token)
                logger.info("save_config: DPAPI save_token 成功，token 已写入 credential.bin")  # 为什么打这条日志：确认 DPAPI 加密保存成功
            except Exception as e:
                logger.error("save_config: DPAPI save_token 失败: %s", e)  # 为什么打这条日志：记录 DPAPI 保存失败，排查凭据保存问题
            # 2) config.json 里只存占位符，避免两份明文不一致的迷惑，同时保持 View 读取逻辑兼容
            cfg_new["token"] = "******"
            changed = True
        # 若提供了 server_url/token 但没有 agent_id，自动生成并持久化
        if changed and not cfg_new.get("agent_id"):
            new_agent_id = get_agent_id()
            logger.info("save_config: 自动生成 agent_id，前缀前 8 位: %s", str(new_agent_id)[:8])  # 为什么打这条日志：记录 agent_id 自动生成（仅记前缀，脱敏）
            cfg_new["agent_id"] = new_agent_id
            changed = True

        if changed:
            save_config(cfg_new)
            logger.info("save_config: 配置已成功保存到 config.json")  # 为什么打这条日志：确认 config.json 保存成功
        elif token == "" and "token" in cfg_new:
            # 用户清空了 token → 删除 credential.bin 解绑本机
            logger.info("save_config: 用户清空 token，删除 credential.bin 解绑本机")  # 为什么打这条日志：记录 token 清空解绑操作
            delete_token()
            cfg_new.pop("token", None)
            save_config(cfg_new)

        show_notify("设置已保存", "SAU 设置")
        self._view.close()
