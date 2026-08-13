"""
sau_tray.controllers.service_ctrl
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
服务控制器。

管理服务启停、状态轮询、图标更新、升级提示和退出清理。
从 tray_app.py 提取的业务逻辑。
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from sau_tray.core import gui_thread
from sau_tray.core.config import (
    APP_NAME, LOCAL_API_URL, PLATFORM_DISPLAY_NAMES, POLL_INTERVAL,
    SAU_HOME, _APP_VERSION, _PROJECT_ROOT,
)
from sau_tray.core import state
from sau_tray.services import machine_id, system_svc
from sau_tray.views.dialogs import show_notify, show_info

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 图标绘制（Pillow 动态绘制）
# ---------------------------------------------------------------------------
def _create_icon_image(color: str) -> Any:
    """用 Pillow 绘制纯色圆形图标（32×32）。"""
    from PIL import Image, ImageDraw

    size = 32
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, size - 3, size - 3], fill=color, outline="white", width=1)
    return img


def _color_for_status(status: dict[str, Any]) -> str:
    """根据状态返回图标颜色。"""
    if status.get("updating"):
        return "#FFAA00"
    if not status.get("service_running"):
        return "#FF4444"
    if not status.get("ws_connected"):
        return "#FFAA00"
    if status.get("clock_sync_status") == "drifted":
        return "#FFAA00"
    if status.get("token_status") in ("expiring", "grace", "expired"):
        return "#FFAA00"
    return "#44CC44"


# ---------------------------------------------------------------------------
# 本地 API 辅助
# ---------------------------------------------------------------------------
def _get_local_token() -> str:
    """读取 local_token.bin。"""
    try:
        from sau_agent_pkg.config import load_local_token
        token = load_local_token() or ""
        return token
    except Exception as e:
        logger.warning("_get_local_token: 读取 local_token 失败: %s", e)  # 为什么打这条日志：记录本地 token 读取失败，排查本地 API 鉴权问题
        return ""


def _fetch_local_api(path: str, timeout: float = 2) -> dict[str, Any]:
    """调用本地 API GET 接口（不可达时抛异常）。"""
    import urllib.request
    import json

    req = urllib.request.Request(f"{LOCAL_API_URL}{path}", method="GET")
    req.add_header("X-SAU-Local-Token", _get_local_token())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class ServiceController:
    """服务控制器。

    封装 Windows 服务启停逻辑、状态轮询、图标更新、升级提示和退出清理。
    """

    def __init__(self) -> None:
        logger.info("ServiceController 构造函数开始初始化")  # 为什么打这条日志：追踪 ServiceController 初始化
        self._cleanup_done = False
        self._icon: object | None = None
        # 升级状态
        self._upgrade_state_lock = threading.Lock()
        self._current_upgrade_state: dict[str, Any] = {}
        self._prompted_versions: set[str] = set()
        self._applying_target_version: str | None = None
        # 后台线程控制
        self._poll_thread: threading.Thread | None = None
        self._updater_thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        logger.info("ServiceController 构造函数初始化完成")  # 为什么打这条日志：确认 ServiceController 构造成功

    def __del__(self) -> None:
        logger.info("ServiceController 析构函数被调用")  # 为什么打这条日志：追踪 ServiceController 生命周期结束

    def set_icon(self, icon: object) -> None:
        """设置托盘图标引用（供异步操作发送通知）。"""
        logger.info("ServiceController.set_icon: 注入托盘图标引用")  # 为什么打这条日志：确认图标引用注入时机，排查异步通知发送失败
        self._icon = icon

    # ------------------------------------------------------------------
    # 状态查询（供 tray_app 菜单回调使用）
    # ------------------------------------------------------------------
    @property
    def icon(self) -> object | None:
        return self._icon

    def get_upgrade_state(self) -> dict[str, Any]:
        with self._upgrade_state_lock:
            return dict(self._current_upgrade_state)

    def is_applying(self, upgrade_state: dict[str, Any]) -> bool:
        return upgrade_state.get("phase") == "applying" or bool(self._applying_target_version)

    # ------------------------------------------------------------------
    # 后台线程：状态轮询 + 图标更新
    # ------------------------------------------------------------------
    def start_background_threads(self) -> threading.Event:
        """启动状态轮询和图标更新后台线程，返回 stop_event。"""
        logger.info("start_background_threads: 创建 stop_event")  # 为什么打这条日志：记录后台线程启动的起点
        self._stop_event = threading.Event()

        thread_names = []
        self._poll_thread = threading.Thread(
            target=self._poll_status_loop,
            args=(self._stop_event,),
            daemon=True,
            name="SAU-StatusPoll",
        )
        self._poll_thread.start()
        thread_names.append(self._poll_thread.name)
        logger.info("start_background_threads: 启动状态轮询线程 %s", self._poll_thread.name)  # 为什么打这条日志：确认轮询线程已启动

        if self._icon is not None:
            self._updater_thread = threading.Thread(
                target=self._icon_updater_loop,
                args=(self._icon, self._stop_event),
                daemon=True,
                name="SAU-IconUpdater",
            )
            self._updater_thread.start()
            thread_names.append(self._updater_thread.name)
            logger.info("start_background_threads: 启动图标更新线程 %s", self._updater_thread.name)  # 为什么打这条日志：确认图标更新线程已启动
        else:
            logger.info("start_background_threads: icon 未注入，跳过图标更新线程")  # 为什么打这条日志：记录图标更新线程被跳过的情况

        logger.info("start_background_threads: 完成，已启动线程列表=%s", thread_names)  # 为什么打这条日志：汇总后台线程启动结果

        return self._stop_event

    def stop_background_threads(self) -> None:
        """停止后台线程。"""
        if self._stop_event is not None:
            self._stop_event.set()

    # ------------------------------------------------------------------
    # 服务控制
    # ------------------------------------------------------------------
    def start_service(self) -> None:
        """启动 Windows 服务（异步执行，避免阻塞 pystray 线程）。"""
        logger.info("on_start_service: 执行启动服务命令")  # 为什么打这条日志：追踪用户启动服务的操作
        def _do_start() -> None:
            try:
                system_svc.elevated_service_control("start")
                logger.info("on_start_service: 提权成功，start 命令已发出")  # 为什么打这条日志：确认 UAC 提权通过
                self._wait_for_status("running")
                logger.info("on_start_service: 服务已达到 running 状态")  # 为什么打这条日志：确认服务启动成功
                show_notify("服务已启动")
            except Exception as e:
                logger.warning("on_start_service: 提权失败或执行异常: %s", e)  # 为什么打这条日志：记录启动失败原因（含 UAC 拒绝）
                show_notify(f"启动服务失败: {e}")
        threading.Thread(target=_do_start, daemon=True, name="SAU-SvcStart").start()

    def stop_service(self) -> None:
        """停止 Windows 服务（异步执行，避免阻塞 pystray 线程）。"""
        logger.info("on_stop_service: 执行停止服务命令")  # 为什么打这条日志：追踪用户停止服务的操作
        def _do_stop() -> None:
            try:
                system_svc.elevated_service_control("stop")
                logger.info("on_stop_service: 提权成功，stop 命令已发出")  # 为什么打这条日志：确认 UAC 提权通过
                self._wait_for_status("stopped")
                logger.info("on_stop_service: 服务已达到 stopped 状态")  # 为什么打这条日志：确认服务停止成功
                show_notify("服务已停止")
            except Exception as e:
                logger.warning("on_stop_service: 提权失败或执行异常: %s", e)  # 为什么打这条日志：记录停止失败原因（含 UAC 拒绝）
                show_notify(f"停止服务失败: {e}")
        threading.Thread(target=_do_stop, daemon=True, name="SAU-SvcStop").start()

    def restart_service(self) -> None:
        """重启 Windows 服务（异步执行，避免阻塞 pystray 线程）。"""
        logger.info("on_restart_service: 执行重启服务命令")  # 为什么打这条日志：追踪用户重启服务的操作
        def _do_restart() -> None:
            try:
                system_svc.elevated_service_control("stop")
                logger.info("on_restart_service: stop 阶段提权成功")  # 为什么打这条日志：确认停止阶段 UAC 通过
                self._wait_for_status("stopped")
                time.sleep(1)
                system_svc.elevated_service_control("start")
                logger.info("on_restart_service: start 阶段提权成功")  # 为什么打这条日志：确认启动阶段 UAC 通过
                self._wait_for_status("running")
                logger.info("on_restart_service: 服务已重启完成，状态 running")  # 为什么打这条日志：确认服务重启成功
                show_notify("服务已重启")
            except Exception as e:
                logger.warning("on_restart_service: 提权失败或执行异常: %s", e)  # 为什么打这条日志：记录重启失败原因
                show_notify(f"重启服务失败: {e}")
        threading.Thread(target=_do_restart, daemon=True, name="SAU-SvcRestart").start()

    # ------------------------------------------------------------------
    # 菜单回调（从 tray_app.py 移入）
    # ------------------------------------------------------------------
    def on_open_logs(self, icon: Any, item: Any) -> None:
        """打开日志目录。"""
        logs_dir = SAU_HOME / "logs"
        logger.info("on_open_logs: 请求打开日志目录 path=%s", logs_dir)  # 为什么打这条日志：追踪用户打开日志目录的操作
        logs_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(logs_dir))
            logger.info("on_open_logs: 已成功打开日志目录")  # 为什么打这条日志：确认日志目录打开成功
        except Exception as e:
            logger.warning("on_open_logs: 打开日志目录失败: %s", e)  # 为什么打这条日志：记录目录打开失败，排查关联问题

    def on_about(self, icon: Any, item: Any) -> None:
        """关于对话框。"""
        logger.info("on_about: 用户点击关于菜单")  # 为什么打这条日志：追踪用户打开关于对话框的操作
        status = state.get_status()
        mc = machine_id.get_machine_code()
        token_expire_text = self._format_token_expire(status.get("token_expire_at"))
        remaining_days = status.get("token_remaining_days")
        if remaining_days is None:
            token_remain_text = "永久"
        else:
            token_remain_text = f"{remaining_days:.1f} 天"

        msg = (
            f"{APP_NAME} v{_APP_VERSION}\n\n"
            f"Agent ID: {status.get('agent_id', 'N/A')}\n"
            f"机器码: {mc}\n"
            f"WS 连接: {'已连接' if status.get('ws_connected') else '未连接'}\n"
            f"活跃任务: {status.get('active_tasks', 0)}\n"
            f"时钟偏差: {status.get('clock_offset_seconds', 0):.1f}s\n"
            f"Token 到期: {token_expire_text}（剩余 {token_remain_text}）\n"
            f"SAU_HOME: {SAU_HOME}"
        )
        show_notify(msg, "关于 SAU Agent")

    def on_show_accounts(self, icon: Any, item: Any) -> None:
        """弹窗显示账号状态。"""
        logger.info("on_show_accounts: 请求 /accounts/status，显示账号状态")  # 为什么打这条日志：追踪用户查看账号状态的操作
        try:
            fetched = _fetch_local_api("/accounts/status")
            accounts = fetched.get("accounts", []) if isinstance(fetched, dict) else []
            logger.info("on_show_accounts: 成功获取账号列表，共 %d 个", len(accounts))  # 为什么打这条日志：确认账号状态获取成功
        except Exception as e:
            logger.warning("on_show_accounts: 获取账号状态失败，使用缓存: %s", e)  # 为什么打这条日志：记录接口失败，排查本地 API 问题
            status = state.get_status()
            accounts = status.get("accounts", [])
        if not accounts:
            show_notify("暂无已登录账号")
            return
        lines = ["已登录账号：\n"]
        for acc in accounts:
            platform = PLATFORM_DISPLAY_NAMES.get(acc.get("platform_key", ""), acc.get("platform_key", ""))
            name = acc.get("account_name", "unknown")
            is_valid = acc.get("is_valid")
            # 显示有效性：None=未检查，True=有效，False=失效
            if is_valid is None:
                valid_tag = "(未检查)"
            elif is_valid:
                valid_tag = "(有效)"
            else:
                valid_tag = "(失效)"
            lines.append(f"  • {platform} / {name} {valid_tag}")
            logger.debug("on_show_accounts: 显示账号 %s / %s is_valid=%s", platform, name, is_valid)  # 为什么打这条日志：确认账号状态显示成功
        # 为什么不用 show_notify：Windows Toast 通知有行数/高度限制，超过 2 条账号会被截断，
        # 出现"实际上有 3 条（含小红书）但弹框只显示 2 条"的视觉错觉。
        # MessageBoxW（show_info 内部实现）无行数限制，可滚动显示全部内容。
        show_info("\n".join(lines), "账号状态")

    def on_recheck_accounts(self, icon: Any, item: Any) -> None:
        """触发全量账号检查：POST 返回 task_id → 轮询 /accounts/status 拿最终结果。

        为什么不直接同步等待 /accounts/recheck：
        - 检查单账号 ~10-25s，多账号会超过 HTTP timeout 10s，
          导致 GUI 看到“账号检查失败: time out”的假象。
        - 改成“提交后台 + 轮询进度”后，不会再撞 HTTP 超时；
          且进度过程中可以弹“检查中 … X/Y 已完成”，用户体验更好。
        """
        logger.info("on_recheck_accounts: 用户触发全量账号检查")  # 为什么打这条日志：追踪用户触发账号检查的操作

        def _do_recheck() -> None:
            import urllib.request
            import urllib.error
            import json
            import time

            token = _get_local_token()
            # 1. 提交后台任务
            try:
                req = urllib.request.Request(
                    f"{LOCAL_API_URL}/accounts/recheck",
                    method="POST", data=b"",
                )
                req.add_header("X-SAU-Local-Token", token)
                # 这里 timeout 可以给短一些（提交操作几乎瞬时）
                with urllib.request.urlopen(req, timeout=5) as resp:
                    submit = json.loads(resp.read().decode("utf-8"))
                task_id = submit.get("task_id", "")
                logger.info("on_recheck_accounts: 已提交后台任务 task_id=%s", task_id)  # 为什么打这条日志：确认后台检查任务已提交
            except Exception as e:
                logger.error("on_recheck_accounts: 提交账号检查任务失败: %s", e)  # 为什么打这条日志：记录任务提交失败原因
                show_notify(f"账号检查失败: {e}")
                return

            if not task_id:
                logger.error("on_recheck_accounts: 服务未返回 task_id")  # 为什么打这条日志：记录服务端返回异常
                show_notify("账号检查失败: 服务未返回 task_id")
                return

            # 2. 轮询进度：最多 15 分钟（大量账号 + 网络抖动），每 2s 查一次
            #    整体超过 15min 也按超时提示“检查超时，稍后在账号状态查看最近一次”
            max_wait_sec = 15 * 60
            poll_interval_sec = 2
            deadline = time.time() + max_wait_sec
            last_done = -1
            last_status_name = ""
            while time.time() < deadline:
                try:
                    status_req = urllib.request.Request(
                        f"{LOCAL_API_URL}/accounts/status?task_id={task_id}",
                        method="GET",
                    )
                    status_req.add_header("X-SAU-Local-Token", token)
                    with urllib.request.urlopen(status_req, timeout=3) as resp:
                        status = json.loads(resp.read().decode("utf-8"))
                except urllib.error.URLError:
                    # 临时网络抖动，继续等
                    time.sleep(poll_interval_sec)
                    continue
                except Exception as e:
                    logger.error("on_recheck_accounts: 检查进度查询失败: %s", e)  # 为什么打这条日志：记录轮询阶段异常
                    show_notify(f"检查进度查询失败: {e}")
                    return

                status_name = status.get("status", "unknown")
                done = int(status.get("done", 0))
                total = int(status.get("total", 0)) or None
                if status_name != last_status_name or done != last_done:
                    logger.info("on_recheck_accounts: 轮询状态变化 status=%s, done=%s, total=%s",  # 为什么打这条日志：记录每次状态/进度变化，追踪检查流程进展
                                status_name, done, total)
                    last_status_name = status_name
                if status_name == "running" and done != last_done:
                    # 每完成一个账号弹一次太吵，只在数量变化时提示
                    # 进度单行提示不会触发 Toast 行数截断，继续用 show_notify 即可
                    if total:
                        show_notify(f"账号检查中 … {done}/{total} 已完成")
                    else:
                        show_notify(f"账号检查中 … 已完成 {done}")
                    last_done = done
                if status_name in ("done", "failed", "cached"):
                    accounts = status.get("accounts", []) or []
                    count = len(accounts)
                    valid = sum(1 for a in accounts if a.get("is_valid"))
                    invalid = count - valid
                    logger.info("on_recheck_accounts: 检查结束 status=%s, 总数=%d, 有效=%d, 失效=%d",  # 为什么打这条日志：汇总账号检查最终结果
                                status_name, count, valid, invalid)
                    if status_name == "failed":
                        err = status.get("error") or ""
                        show_notify(f"账号检查异常: {err or 'unknown'}（完成 {count} 个）")
                    else:
                        # 结果详情（平台/账号名/有效失效）多行，Toast 会截断，改用 show_info 弹框
                        lines = [f"账号检查完成，共 {count} 个（有效 {valid}，失效 {invalid}）：\n"]
                        for acc in accounts:
                            platform = PLATFORM_DISPLAY_NAMES.get(acc.get("platform_key", ""), acc.get("platform_key", ""))
                            name = acc.get("account_name", "unknown")
                            tag = "✅" if acc.get("is_valid") else "❌"
                            lines.append(f"  {tag} {platform} / {name}")
                        show_info("\n".join(lines), "账号检查结果")
                    return
                time.sleep(poll_interval_sec)

            # 超时：给一个不阻塞 UI 的弱提示
            logger.warning("on_recheck_accounts: 检查超时（超过 %ds），仍在后台运行", max_wait_sec)  # 为什么打这条日志：记录超时情况
            show_notify("账号检查仍在后台进行中，稍后在「账号状态」查看结果")

        threading.Thread(target=_do_recheck, daemon=True, name="SAU-Recheck").start()

    def on_check_update(self, icon: Any, item: Any) -> None:
        """检查更新：显示当前版本 + 更新状态。"""
        logger.info("on_check_update: 用户点击检查更新菜单")  # 为什么打这条日志：追踪用户检查更新的操作
        try:
            fetched = _fetch_local_api("/upgrade")
            upgrade_state = fetched if isinstance(fetched, dict) else {}
            logger.info("on_check_update: 调 /upgrade 成功，phase=%s, version=%s",  # 为什么打这条日志：确认获取升级状态成功
                        upgrade_state.get("phase"), upgrade_state.get("version"))
        except Exception as e:
            logger.warning("on_check_update: 调 /upgrade 失败: %s，使用缓存状态", e)  # 为什么打这条日志：记录本地 API 异常
            upgrade_state = self.get_upgrade_state()
        phase = upgrade_state.get("phase")
        version = str(upgrade_state.get("version") or "")

        if phase in ("noticed", "downloading"):
            show_notify(f"新版本 v{version} 下载中，请稍候", "检查更新")
        elif phase == "ready":
            # GUI 线程异步确认框 → 确认后拉起升级（不阻塞菜单/轮询线程）
            self._prompt_confirm_upgrade(upgrade_state, version)
        elif phase == "applying":
            show_notify("正在更新，请稍候…", "检查更新")
        elif phase == "failed":
            show_notify(f"更新到 v{version} 失败，可稍后重试或等待下次推送", "检查更新")
        elif phase == "rolled_back":
            show_notify(f"更新到 v{version} 失败，已回滚到旧版本", "检查更新")
        else:
            show_notify(f"当前已是最新版本 (v{_APP_VERSION})", "检查更新")

    def on_launch_upgrade(self, icon: Any, item: Any) -> None:
        """启动升级编排。"""
        upgrade_state = self.get_upgrade_state()
        try:
            self._launch_upgrade(upgrade_state)
        except Exception as e:
            show_notify(f"启动更新失败: {e}")

    # ------------------------------------------------------------------
    # 退出清理
    # ------------------------------------------------------------------
    def on_exit(self, icon: object) -> None:
        """退出清理：关闭 GUI/托盘 → 停止服务（带等待与超时保护） → 终止进程 → 释放锁。

        关键约束：无论 UAC 是否确认、服务是否停得下来，托盘都必须能退出，
        绝不永久挂起。
        """
        logger.info("on_exit: 开始退出清理")

        # 1. 停止后台线程
        logger.info("on_exit 清理步骤[1/6]: 停止后台轮询线程（调用 stop_background_threads）")  # 为什么打这条日志：追踪清理步骤顺序
        self.stop_background_threads()

        # 2. 停止 GUI 线程
        logger.info("on_exit 清理步骤[2/6]: 停止 GUI 线程（调用 gui_thread.stop）")  # 为什么打这条日志：追踪清理步骤顺序
        try:
            gui_thread.stop()
        except Exception:
            logger.exception("on_exit: 停止 GUI 线程异常")

        # 3. 等待登录线程结束
        logger.info("on_exit 清理步骤[3/6]: 等待登录线程结束（轮询线程 join 2s）")  # 为什么打这条日志：追踪清理步骤顺序
        try:
            for t in gui_thread.get_login_threads():
                t.join(timeout=2.0)
        except Exception:
            logger.exception("on_exit: 等待登录线程异常")

        # 4. 停服：elevated_service_control 会阻塞在 UAC 确认框，
        #    故放入独立线程并 join(15) 防护；超时/UAC 拒绝均不阻塞退出。
        logger.info("on_exit 清理步骤[4/6]: 停止服务（停服+轮询确认，带超时防护）")  # 为什么打这条日志：追踪清理步骤顺序
        stop_error: dict[str, Any] = {}

        def _do_stop() -> None:
            try:
                system_svc.elevated_service_control("stop")
            except Exception as e:  # 含 UAC 拒绝（ShellExecute code=5）
                stop_error["error"] = e

        stop_thread = threading.Thread(
            target=_do_stop, daemon=True, name="SAU-Exit-StopSvc",
        )
        stop_thread.start()
        stop_thread.join(timeout=15)

        if stop_thread.is_alive():
            logger.warning("on_exit: 等待 UAC 确认停服超时(15s)，继续兜底清理")
            try:
                show_notify("UAC 未确认，服务可能未停止")
            except Exception:
                logger.exception("on_exit: UAC 超时提示发送失败")
        elif stop_error.get("error") is not None:
            logger.warning("on_exit: 停服请求失败（可能 UAC 拒绝）: %r", stop_error["error"])
            try:
                show_notify("UAC 未确认，服务可能未停止")
            except Exception:
                logger.exception("on_exit: UAC 拒绝提示发送失败")
        else:
            # stop 只发控制码立即返回，需轮询确认实际停机（与“停止服务”菜单一致）
            # 先查一次状态：服务未安装/状态不可查/已停止时无需空等 30s
            from sau_service.service_host import get_service_status
            need_wait = True
            try:
                current = get_service_status()
                if current == "stopped" or current.startswith("not installed") or current.startswith("unknown"):
                    logger.info("on_exit: 服务当前状态 %s，无需等待停机", current)
                    need_wait = False
            except Exception:
                logger.exception("on_exit: 停机前查询服务状态异常，按需要等待处理")
            if need_wait:
                logger.info("on_exit: 停服控制码已发出，轮询等待服务停止")
                try:
                    self._wait_for_status("stopped", timeout=30)
                    logger.info("on_exit: 服务已确认停止")
                except Exception as e:
                    logger.warning("on_exit: 服务 30s 内未停止，继续兜底清理: %s", e)
                    try:
                        show_notify("服务未能在 30 秒内停止，继续退出清理")
                    except Exception:
                        logger.exception("on_exit: 停服超时提示发送失败")

        # 5. 进程清理（互斥锁延后到 icon.stop() 之后释放，见步骤 7）
        logger.info("on_exit 清理步骤[5/6]: 清理 sau 进程（kill_sau_processes）+ 停止托盘 icon.stop")  # 为什么打这条日志：追踪清理步骤顺序
        try:
            cleanup_lock = gui_thread.get_cleanup_lock()
            with cleanup_lock:
                if not gui_thread.is_cleanup_done():
                    logger.info("on_exit: 执行 kill_sau_processes() 清理残留进程")  # 为什么打这条日志：确认进程清理已触发
                    system_svc.kill_sau_processes()
                    gui_thread.set_cleanup_done()
            logger.info("on_exit: 进程清理完成")
        except Exception:
            logger.exception("on_exit: 进程清理异常")

        # 6. 停止托盘图标
        try:
            logger.info("on_exit: 执行 icon.stop() 停止托盘图标")  # 为什么打这条日志：确认托盘停止步骤已执行
            icon.stop()
        except Exception:
            logger.exception("on_exit: 停止托盘图标异常")

        # 7. 释放单实例互斥锁（尽量靠后：icon.stop() 异常时避免无锁窗口
        #    导致用户重启出双实例）
        logger.info("on_exit 清理步骤[6/6]: 释放单实例互斥锁 release_single_instance")  # 为什么打这条日志：追踪清理步骤顺序
        try:
            system_svc.release_single_instance()
            logger.info("on_exit: 单实例互斥锁已释放")
        except Exception:
            logger.exception("on_exit: 释放互斥锁异常")

        logger.info("on_exit: 退出清理完成")

    # ------------------------------------------------------------------
    # 内部：状态轮询
    # ------------------------------------------------------------------
    def _poll_status_loop(self, stop_event: threading.Event) -> None:
        """后台线程：每 3s 轮询本地 API /status，同一循环顺带 GET /upgrade。"""
        while not stop_event.is_set():
            try:
                fetched = _fetch_local_api("/upgrade")
                upgrade_state = fetched if isinstance(fetched, dict) else {}
            except Exception:
                upgrade_state = self.get_upgrade_state()
            with self._upgrade_state_lock:
                self._current_upgrade_state = upgrade_state
            self._track_applying(upgrade_state)

            try:
                data = _fetch_local_api("/status")
                data["service_running"] = True
                data["updating"] = False
                state.set_status(data)
                self._check_apply_result(data)
            except Exception:
                state.set_status({
                    "service_running": False,
                    "ws_connected": False,
                    "clock_sync_status": "unknown",
                    "updating": self.is_applying(upgrade_state),
                })

            self._maybe_prompt_upgrade(upgrade_state)
            stop_event.wait(POLL_INTERVAL)

    # ------------------------------------------------------------------
    # 内部：图标更新
    # ------------------------------------------------------------------
    def _icon_updater_loop(self, icon: Any, stop_event: threading.Event) -> None:
        """后台线程：根据状态更新图标颜色。"""
        last_color = ""
        while not stop_event.is_set():
            status = state.get_status()
            color = _color_for_status(status)
            if color != last_color:
                try:
                    icon.icon = _create_icon_image(color)
                    if status.get("updating"):
                        icon.title = f"{APP_NAME}  ◐ 正在更新，服务将短暂中断"
                    elif status.get("ws_connected"):
                        icon.title = f"{APP_NAME}  ● 已连接"
                    elif status.get("service_running"):
                        icon.title = f"{APP_NAME}  ○ 未连接"
                    else:
                        icon.title = f"{APP_NAME}  ✕ 服务未运行"
                    token_status = status.get("token_status", "unknown")
                    if token_status in ("expiring", "grace", "expired"):
                        remaining_days = status.get("token_remaining_days")
                        if token_status == "expired":
                            icon.title += "  ⚠ token 已过期"
                        elif remaining_days is not None:
                            icon.title += f"  ⚠ token 剩余 {remaining_days:.0f} 天"
                        else:
                            icon.title += "  ⚠ token 即将到期"
                    last_color = color
                except Exception:
                    pass
            stop_event.wait(2)

    # ------------------------------------------------------------------
    # 内部：升级管理
    # ------------------------------------------------------------------
    def _track_applying(self, upgrade_state: dict[str, Any]) -> None:
        """观察 applying 阶段，记录目标版本。"""
        if upgrade_state.get("phase") == "applying" and upgrade_state.get("version"):
            self._applying_target_version = str(upgrade_state["version"])

    def _check_apply_result(self, status: dict[str, Any]) -> None:
        """服务恢复可达后，比对版本并弹出更新结果。

        版本不匹配时按 upgrade_state 实际 phase 出文案：编排可能在写
        applying 之前就中止（非管理员/安装包缺失/SHA 不匹配），不能一律
        报“版本异常可能已回滚”。
        """
        if not self._applying_target_version:
            return
        target = self._applying_target_version
        self._applying_target_version = None
        actual = str(status.get("version") or "")
        if actual == target:
            show_notify(f"更新成功：当前版本 v{actual}")
            return
        phase = str(self.get_upgrade_state().get("phase") or "")
        if phase == "rolled_back":
            show_notify(
                f"更新到 v{target} 失败，已自动回滚到旧版本（当前 v{actual or '未知'}）"
            )
        elif phase == "failed":
            show_notify(
                f"更新到 v{target} 未执行或失败（当前版本 v{actual or '未知'}），可稍后重试"
            )
        else:
            show_notify(
                f"更新结束但版本不匹配（期望 v{target}，实际 v{actual or '未知'}），请检查更新日志"
            )

    def _maybe_prompt_upgrade(self, upgrade_state: dict[str, Any]) -> None:
        """phase=ready 且本版本未弹过 → 通知用户有新版本。"""
        if upgrade_state.get("phase") != "ready":
            return
        version = str(upgrade_state.get("version") or "")
        if not version or version in self._prompted_versions:
            return
        self._prompted_versions.add(version)
        self._prompt_confirm_upgrade(upgrade_state, version)

    def _prompt_confirm_upgrade(self, upgrade_state: dict[str, Any], version: str) -> None:
        """确认框调度到 GUI 线程异步执行（不阻塞轮询/菜单线程）。

        用户在确认框上的等待不影响轮询；结果回调中再执行 _launch_upgrade
        （文件复制+提权在工作线程，避免阻塞 tkinter mainloop）。

        为什么用 show_confirm 而不是 tkinter.messagebox.askyesno：
        - show_confirm 内部统一通过 gui_thread.schedule 切 GUI 线程 + 屏幕居中，
          同时 show_confirm 是 fire-and-wait（调用方等结果），对 _prompt_confirm_upgrade
          来说，由于是异步触发（轮询/菜单线程调用，且不在主线程等待），
          我们启动独立线程等待结果，避免卡住调用方。
        - 统一走 show_confirm 后，所有弹框的视觉效果（居中、MB 图标、模态级别）
          都一致，不会出现 tk 确认框偏右下角、而 show_info 在屏幕中央的分裂感。
        """
        def _on_confirm_yes() -> None:
            try:
                self._launch_upgrade(self.get_upgrade_state() or upgrade_state)
            except Exception as e:
                show_notify(f"启动更新失败: {e}")

        def _wait_and_handle() -> None:
            try:
                from sau_tray.views.dialogs import show_confirm
                yes = show_confirm(
                    f"发现新版本 v{version}，是否立即更新？",
                    APP_NAME,
                )
            except Exception as e:
                logger.warning("更新确认框异常: %s", e)
                return
            if yes:
                threading.Thread(
                    target=_on_confirm_yes, daemon=True, name="SAU-LaunchUpgrade",
                ).start()

        gt = gui_thread.get_thread()
        if gt is None or not gt.is_alive():
            show_notify(
                f"发现新版本 v{version}，请稍后通过托盘菜单「检查更新」进行安装"
            )
            return
        # show_confirm 会等待用户点击，用独立线程避免阻塞菜单/轮询线程
        threading.Thread(target=_wait_and_handle, daemon=True, name="SAU-UpgradeAsk").start()

    def _launch_upgrade(self, upgrade_state: dict[str, Any]) -> None:
        """复制 sau-ops.exe 并提权启动升级编排。"""
        installer_path = str(upgrade_state.get("installer_path") or "")
        version = str(upgrade_state.get("version") or "")
        if not installer_path or not version:
            raise RuntimeError("更新状态缺少 installer_path/version")

        import shutil
        sau_ops_src = Path(_PROJECT_ROOT) / "sau-ops.exe"
        if not sau_ops_src.is_file():
            raise FileNotFoundError(f"找不到 sau-ops.exe: {sau_ops_src}")
        runner_dir = SAU_HOME / "updates" / "runner"
        runner_dir.mkdir(parents=True, exist_ok=True)
        runner_exe = runner_dir / "sau-ops.exe"
        shutil.copy2(sau_ops_src, runner_exe)

        params = f'service upgrade --installer "{installer_path}" --target-version "{version}"'
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", str(runner_exe), params, None, 1,
        )
        if result <= 32:
            raise RuntimeError(f"ShellExecute 失败 (code={result})")

        self._applying_target_version = version
        show_notify(f"已开始更新到 v{version}，服务将短暂中断")

    # ------------------------------------------------------------------
    # 内部：辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _format_token_expire(expire_at_ms: Any) -> str:
        """将毫秒时间戳格式化为可读日期；None 表示永久。"""
        if expire_at_ms is None:
            return "永久"
        try:
            from datetime import datetime
            return datetime.fromtimestamp(expire_at_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError):
            return str(expire_at_ms)

    @staticmethod
    def _wait_for_status(target_status: str, timeout: int = 30) -> None:
        """轮询等待服务达到目标状态。"""
        from sau_service.service_host import get_service_status
        deadline = time.time() + timeout
        while time.time() < deadline:
            current = get_service_status()
            if current == target_status:
                return
            time.sleep(1)
        raise TimeoutError(
            f"服务未能在 {timeout}s 内达到 {target_status} 状态"
            f"（当前: {get_service_status()}）"
        )
