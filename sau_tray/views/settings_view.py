"""
sau_tray.views.settings_view
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
设置窗口视图。

从 tray_app.py 的 _create_settings_window() 提取。
通过回调函数与 Controller 通信，不直接引用 Controller。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from sau_tray.core import gui_thread
from sau_tray.views.base import BaseView

logger = logging.getLogger(__name__)


class SettingsView(BaseView):
    """设置窗口：机器码显示 + 服务器地址 + Token 配置。

    Parameters
    ----------
    on_test_connection : callable
        签名 (server_url, token, status_callback) → None
        在后台线程执行连接测试，通过 status_callback 回传结果。
    on_save_config : callable
        签名 (server_url, token) → None
        保存配置。
    """

    def __init__(
        self,
        on_test_connection: Callable,
        on_save_config: Callable,
    ):
        super().__init__()
        logger.info("SettingsView 初始化完成")  # 为什么打这条日志：追踪 SettingsView 生命周期
        self._on_test_connection = on_test_connection
        self._on_save_config = on_save_config
        # tkinter 变量（在 _create 中初始化）
        self._status_var = None
        self._url_var = None
        self._token_var = None
        self._token_entry = None
        self._show_token_btn = None
        self._copy_btn = None

    def show(self) -> None:
        """显示设置窗口。"""
        logger.info("SettingsView.show: 打开设置窗口")  # 为什么打这条日志：追踪设置窗口打开时机
        super().show()

    def close(self) -> None:
        """关闭设置窗口。"""
        logger.info("SettingsView.close: 关闭设置窗口")  # 为什么打这条日志：追踪设置窗口关闭时机
        super().close()

    # ------------------------------------------------------------------
    # 公开 API（供 Controller 调用）
    # ------------------------------------------------------------------
    def set_status(self, text: str) -> None:
        """更新连接测试状态文本（可从任意线程调用，强制通过 GUI 线程调度）。"""
        def _update(root):
            if self._status_var is not None:
                self._status_var.set(text)
        gui_thread.schedule(_update)

    # ------------------------------------------------------------------
    # 窗口创建
    # ------------------------------------------------------------------
    def _create(self, root: Any) -> None:
        """在 GUI 线程中创建设置窗口（由 gui_thread.schedule 调度）。"""
        logger.info("SettingsView._create: GUI 线程开始创建设置窗口")  # 为什么打这条日志：确认 _create 在 GUI 线程中被调用
        import tkinter as tk
        from tkinter import messagebox
        from sau_agent_pkg.config import load_config
        from sau_tray.services import machine_id

        # 如果已有窗口，先销毁
        if self._window is not None:
            try:
                if self._window.winfo_exists():
                    self._window.destroy()
            except Exception:
                pass
            self._window = None

        # ── 数据准备 ──
        machine_code = machine_id.get_machine_code()
        logger.info("SettingsView._create: 显示机器码（前 8 位）: %s", str(machine_code)[:8])  # 为什么打这条日志：记录机器码显示前缀，帮助核对 Agent 创建时填入的机器码是否匹配
        cfg = load_config()
        current_server_url = cfg.get("server_url", "")
        # 优先从 credential.bin 读取（明文可信度最高），其次回退到 config.json
        # 为什么双路：config.json 里保存的 token 可能是占位符 "******"（不回显明文）
        try:
            from sau_agent_pkg.config import load_token
            current_token = load_token() or cfg.get("token", "") or ""
        except Exception:
            current_token = cfg.get("token", "") or ""

        # ── 构建对话框 ──
        win = tk.Toplevel(root)
        self._window = win
        win.title("SAU 设置")
        win.resizable(False, False)

        # 窗口定位（右下角）
        win_w, win_h = 620, 400
        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        x = screen_w - win_w - 16
        y = screen_h - win_h - 56

        win.withdraw()
        win.geometry(f"{win_w}x{win_h}+{x}+{y}")
        win.configure(padx=16, pady=12)

        # 置顶获取焦点后取消置顶
        win.attributes("-topmost", True)
        win.update_idletasks()
        win.deiconify()
        win.after(300, lambda: win.attributes("-topmost", False))
        win.focus_force()

        win.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── 机器码区域 ──
        tk.Label(
            win, text="机器码（在 opcgeo 后台创建 Agent 时填入）",
            font=("Microsoft YaHei UI", 9, "bold"), anchor="w",
        ).pack(fill="x")

        mc_text = tk.Text(
            win, height=2, wrap="none",
            font=("Consolas", 10), bg="#f5f5f5",
            relief="flat", bd=0, padx=4, pady=4,
        )
        mc_text.insert("1.0", machine_code)
        mc_text.config(state="disabled")
        mc_text.pack(fill="x", pady=(4, 4))

        self._copy_btn = tk.Button(
            win, text="复制机器码", width=12,
            command=lambda: self._copy_machine_code(win, machine_code),
        )
        self._copy_btn.pack(anchor="e", pady=(0, 0))

        # ── 分隔线 ──
        tk.Frame(win, height=1, bg="#cccccc").pack(fill="x", pady=12)

        # ── 服务器地址 ──
        tk.Label(
            win, text="服务器地址（WebSocket）",
            font=("Microsoft YaHei UI", 9, "bold"), anchor="w",
        ).pack(fill="x")

        url_frame = tk.Frame(win)
        url_frame.pack(fill="x", pady=(4, 0))

        self._url_var = tk.StringVar(value=current_server_url)
        url_entry = tk.Entry(
            url_frame, textvariable=self._url_var,
            font=("Consolas", 9),
        )
        url_entry.pack(side="left", fill="x", expand=True)
        url_entry.focus_set()

        # ── 连接状态 ──
        self._status_var = tk.StringVar(value="")

        # ── Token ──
        tk.Label(
            win, text="Agent Token",
            font=("Microsoft YaHei UI", 9, "bold"), anchor="w",
        ).pack(fill="x", pady=(8, 0))

        token_frame = tk.Frame(win)
        token_frame.pack(fill="x", pady=(4, 0))

        self._token_var = tk.StringVar(value=current_token)
        self._token_entry = tk.Entry(
            token_frame, textvariable=self._token_var,
            font=("Consolas", 9), show="*",
        )
        self._token_entry.pack(side="left", fill="x", expand=True)

        self._show_token_btn = tk.Button(
            token_frame, text="显示", width=6,
            command=self._toggle_token_visibility,
        )
        self._show_token_btn.pack(side="right", padx=(6, 0))

        # ── 测试连接按钮 ──
        test_btn = tk.Button(
            url_frame, text="测试连接", width=8,
            command=self._on_test_connection_clicked,
        )
        test_btn.pack(side="right", padx=(6, 0))

        tk.Label(
            win, textvariable=self._status_var, fg="#666666",
            font=("Microsoft YaHei UI", 8), anchor="w",
        ).pack(fill="x", pady=(2, 0))

        tk.Label(
            win,
            text="示例: wss://your-server/opcgeo/agent/ws",
            fg="#999999", font=("Microsoft YaHei UI", 8), anchor="w",
        ).pack(fill="x")

        # ── 底部按钮 ──
        btn_frame = tk.Frame(win)
        btn_frame.pack(fill="x", pady=(12, 0))

        tk.Button(
            btn_frame, text="保存", width=10,
            font=("Microsoft YaHei UI", 9),
            command=self._on_save_clicked,
        ).pack(side="right")

        tk.Button(
            btn_frame, text="取消", width=10,
            font=("Microsoft YaHei UI", 9),
            command=self._on_close,
        ).pack(side="right", padx=(0, 8))

    # ------------------------------------------------------------------
    # 内部事件处理
    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        """窗口关闭回调。"""
        logger.info("SettingsView._on_close: 用户点击取消/关闭按钮")  # 为什么打这条日志：追踪设置窗口取消/关闭操作
        if self._window is not None:
            try:
                if self._window.winfo_exists():
                    self._window.destroy()
            except Exception:
                pass
            self._window = None

    def _copy_machine_code(self, win: Any, machine_code: str) -> None:
        """复制机器码到剪贴板。"""
        logger.info("SettingsView._copy_machine_code: 用户点击复制机器码（前 8 位）: %s", str(machine_code)[:8])  # 为什么打这条日志：追踪机器码复制操作
        win.clipboard_clear()
        win.clipboard_append(machine_code)
        self._copy_btn.config(text="已复制!", state="disabled")
        win.after(1500, lambda: self._copy_btn.config(text="复制机器码", state="normal"))

    def _toggle_token_visibility(self) -> None:
        """切换 Token 显示/隐藏。"""
        if self._token_entry.cget("show") == "*":
            logger.info("SettingsView: 用户切换 Token 为显示状态")  # 为什么打这条日志：记录 Token 显示操作（安全审计点）
            self._token_entry.config(show="")
            self._show_token_btn.config(text="隐藏")
        else:
            logger.info("SettingsView: 用户切换 Token 为隐藏状态")  # 为什么打这条日志：记录 Token 隐藏操作
            self._token_entry.config(show="*")
            self._show_token_btn.config(text="显示")

    def _on_test_connection_clicked(self) -> None:
        """测试连接按钮点击（在 GUI 线程中执行输入校验）。"""
        server_url = self._url_var.get().strip()
        token = self._token_var.get().strip()

        if not server_url:
            logger.warning("SettingsView 测试连接: 输入校验失败，服务器地址为空")  # 为什么打这条日志：记录用户输入校验失败
            self._status_var.set("请先填写服务器地址")
            return
        if not token:
            logger.warning("SettingsView 测试连接: 输入校验失败，Token 为空")  # 为什么打这条日志：记录用户输入校验失败
            self._status_var.set("请先填写 Token")
            return
        if not (server_url.startswith("ws://") or server_url.startswith("wss://")):
            logger.warning("SettingsView 测试连接: 输入校验失败，地址非 ws(s):// 开头: %s", server_url)  # 为什么打这条日志：记录地址格式校验失败
            self._status_var.set("地址应以 ws:// 或 wss:// 开头")
            return

        logger.info("SettingsView: 提交测试连接请求，server_url=%s, token_len=%d", server_url, len(token))  # 为什么打这条日志：记录测试连接提交（token 仅记长度）
        self._status_var.set("正在测试连接...")
        self._on_test_connection(server_url, token, self.set_status)

    def _on_save_clicked(self) -> None:
        """保存按钮点击（在 GUI 线程中执行格式校验）。"""
        from tkinter import messagebox

        logger.info("SettingsView._on_save_clicked: 用户点击保存按钮")  # 为什么打这条日志：追踪保存按钮点击
        new_url = self._url_var.get().strip()
        new_token = self._token_var.get().strip()

        if new_url and not (new_url.startswith("ws://") or new_url.startswith("wss://")):
            logger.warning("SettingsView 保存: 输入校验失败，地址非 ws(s):// 开头: %s", new_url)  # 为什么打这条日志：记录保存时格式校验失败
            messagebox.showwarning(
                "格式错误", "服务器地址应以 ws:// 或 wss:// 开头",
                parent=self._window,
            )
            return

        logger.info("SettingsView: 提交保存配置，server_url=%s, token_len=%d", new_url, len(new_token))  # 为什么打这条日志：记录保存提交参数（token 仅记长度）
        self._on_save_config(new_url, new_token)
