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
        self._exit_in_progress = False  # 为什么加这个：on_exit 调用后会置 True，用户再点菜单里的启动/停止服务/重启/退出时，能正确返回"正在退出，请稍候"而不是重复发起服务控制命令（日志里就出现了退出清理 30s 超时期间又点启动服务，导致 SCM 还在 stopping 时就发 start，必然 30s 后提示启动失败）
        self._exit_lock = threading.Lock()
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
    # 状态同步 / 兜底
    # ------------------------------------------------------------------
    def _refresh_state_from_scm(self, tag: str) -> None:
        """用 SCM 真实状态立刻回写到 state，保证菜单标记与实际一致。

        为什么需要这个函数：
            tray_app.py 的菜单项 callable 文本完全依赖 state["service_running"]。
            但 service_running 只在 _poll_status_loop 每 3s 轮询一次刷新。
            用户刚点「启动服务」1 秒内如果立刻右键菜单，看到的是上一次轮询结果，
            就会出现「停止服务前仍显示 ●」（用户以为没启动成功）的视觉错位。
            另外：_wait_for_status 异常（30s 超时、UAC 拒绝等）也会让 state 停留在旧值，
            必须主动拉一次 SCM 覆盖。
        """
        try:
            from sau_service.service_host import get_service_status
            scm_status = get_service_status()
            # SCM 状态 → service_running 的映射：running / starting → True；其他 False
            scm_to_running = {
                "running": True,
                "starting": True,
                "stopped": False,
                "stopping": False,
            }
            new_service_running = scm_to_running.get(scm_status, None)
            logger.info(
                "_refresh_state_from_scm[%s]: scm_status=%s → state.service_running=%s",
                tag, scm_status, new_service_running,
            )
            state.set_status({
                "service_running": new_service_running,
                "_last_scm_status": scm_status,
                "_last_scm_status_at": time.time(),
            })
        except Exception as e:
            logger.warning(
                "_refresh_state_from_scm[%s]: get_service_status 异常，state 未刷新: %s",
                tag, e,
            )

    @staticmethod
    def _force_kill_sau_service(tag: str) -> None:
        """兜底 taskkill /f /t 杀 sau-service.exe 宿主进程。

        为什么需要这个暴力手段：
            用户日志里出现了 status_code=3 (stopping) 从 00:11:40 一直卡到 00:12:10
            整整 30s 都没有 → stopped。这是典型的服务宿主进程
            自己 asyncio 事件循环关不掉（_call_connection_lost TypeError 等异常）
            导致 SvcDoRun 函数不返回、SCM 一直看到 SERVICE_STOP_PENDING 的状态。
            Windows SCM 不会主动杀进程（直到服务预设时间阈值到了超时），
            所以这里要主动 taskkill tree，让宿主立即退出 → SCM 立刻变 stopped。
        """
        import subprocess
        try:
            logger.warning(
                "_force_kill_sau_service[%s]: 服务长时间卡在 stopping/starting，强制杀 sau-service.exe",
                tag,
            )
            subprocess.run(
                ["taskkill", "/f", "/t", "/im", "sau-service.exe"],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            logger.warning("_force_kill_sau_service[%s] taskkill 失败: %s", tag, e)

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
    def _is_exiting(self, action: str) -> bool:
        """退出清理过程中拦截所有服务控制/菜单操作，避免用户重复点击导致的竞态。

        为什么要拦截：on_exit 开始执行后（步骤 3/7 停服 + 30s 轮询期间），
        用户依然可能点开菜单点击「启动服务」或「退出」，日志里就出现了：
          1) on_exit: 服务 30s 内未停止（stopping）
          2) 用户点击启动服务 → on_start_service 被调用 → elevated_service_control start 发出
          3) SCM 还卡在 stopping → _wait_for_status("running", 30s) → 必然 30s 超时 → 提示启动失败
        拦截后给用户一个"正在退出，请稍候"的 show_notify 就好，不做任何服务控制。
        """
        with self._exit_lock:
            if not self._exit_in_progress:
                return False
        logger.info("ServiceController.%s: 退出清理正在进行，本次操作忽略", action)
        try:
            show_notify("正在退出，请稍候再操作…", APP_NAME)
        except Exception:
            pass
        return True

    def start_service(self) -> None:
        """启动 Windows 服务（异步执行，避免阻塞 pystray 线程）。"""
        if self._is_exiting("start_service"):
            return
        logger.info("on_start_service: 执行启动服务命令")  # 为什么打这条日志：追踪用户启动服务的操作
        def _do_start() -> None:
            try:
                # 启动前先回滚 state：无论之前 service_running 是什么，
                # 用 SCM 真实状态刷新一次，避免菜单文本依赖的状态与 SCM 不一致。
                # 这正是「菜单标记仍是停止服务」的根因：之前 state 里 service_running 没有及时同步。
                self._refresh_state_from_scm("start_service 前")
                system_svc.elevated_service_control("start")
                logger.info("on_start_service: 提权成功，start 命令已发出")  # 为什么打这条日志：确认 UAC 提权通过
                self._wait_for_status("running")
                logger.info("on_start_service: 服务已达到 running 状态")  # 为什么打这条日志：确认服务启动成功
                # 成功后再次刷新 state（含 HTTP /status 结果），保证 3s 轮询间隙里的菜单标记立刻显示 ● 停止服务
                self._refresh_state_from_scm("start_service 后")
                show_notify("服务已启动")
            except Exception as e:
                logger.warning("on_start_service: 提权失败或执行异常: %s", e)  # 为什么打这条日志：记录启动失败原因（含 UAC 拒绝）
                # 启动失败兜底：state.service_running 必须立刻同步为 SCM 真实状态
                # （否则用户看到的菜单标记就还停留在"停止服务"，和实际不一致）
                self._refresh_state_from_scm("start_service 异常")
                show_notify(f"启动服务失败: {e}")
        threading.Thread(target=_do_start, daemon=True, name="SAU-SvcStart").start()

    def stop_service(self) -> None:
        """停止 Windows 服务（异步执行，避免阻塞 pystray 线程）。"""
        if self._is_exiting("stop_service"):
            return
        logger.info("on_stop_service: 执行停止服务命令")  # 为什么打这条日志：追踪用户停止服务的操作
        def _do_stop() -> None:
            try:
                self._refresh_state_from_scm("stop_service 前")
                system_svc.elevated_service_control("stop")
                logger.info("on_stop_service: 提权成功，stop 命令已发出")  # 为什么打这条日志：确认 UAC 提权通过
                self._wait_for_status("stopped")
                logger.info("on_stop_service: 服务已达到 stopped 状态")  # 为什么打这条日志：确认服务停止成功
                self._refresh_state_from_scm("stop_service 后")
                show_notify("服务已停止")
            except Exception as e:
                logger.warning("on_stop_service: 提权失败或执行异常: %s", e)  # 为什么打这条日志：记录停止失败原因（含 UAC 拒绝）
                self._refresh_state_from_scm("stop_service 异常")
                show_notify(f"停止服务失败: {e}")
        threading.Thread(target=_do_stop, daemon=True, name="SAU-SvcStop").start()

    def restart_service(self) -> None:
        """重启 Windows 服务（异步执行，避免阻塞 pystray 线程）。"""
        if self._is_exiting("restart_service"):
            return
        logger.info("on_restart_service: 执行重启服务命令")  # 为什么打这条日志：追踪用户重启服务的操作
        def _do_restart() -> None:
            try:
                self._refresh_state_from_scm("restart_service 前")
                system_svc.elevated_service_control("stop")
                logger.info("on_restart_service: stop 阶段提权成功")  # 为什么打这条日志：确认停止阶段 UAC 通过
                self._wait_for_status("stopped")
                time.sleep(1)
                self._refresh_state_from_scm("restart 中间阶段 stop 完成")
                system_svc.elevated_service_control("start")
                logger.info("on_restart_service: start 阶段提权成功")  # 为什么打这条日志：确认启动阶段 UAC 通过
                self._wait_for_status("running")
                logger.info("on_restart_service: 服务已重启完成，状态 running")  # 为什么打这条日志：确认服务重启成功
                self._refresh_state_from_scm("restart_service 后")
                show_notify("服务已重启")
            except Exception as e:
                logger.warning("on_restart_service: 提权失败或执行异常: %s", e)  # 为什么打这条日志：记录重启失败原因
                self._refresh_state_from_scm("restart_service 异常")
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
        """退出清理：停止后台轮询 → 停止服务 → 清理进程 → 停止 GUI → 停止托盘 → 释放锁。

        关键顺序约束（为什么步骤 4「停止 GUI」被延后到步骤 6）：
          - 步骤 3（服务控制）、步骤 4（清理 sau 进程）中仍需要：
              ① show_notify/show_info（对话框显示必须要 GUI 线程的消息泵）
              ② gui_thread.get_cleanup_lock()（GUI 线程的内部状态，停了就拿不到）
              ③ 升级确认框通过 gui_thread.schedule 调度
          - 如果在步骤 2 就停 GUI 线程，上面这些功能会静默失效
          - 结论：GUI 线程应该是倒数第二个才关（icon.stop 之前或之后均可，但必须在所有"可能弹框/对话框"的业务逻辑之后）

        无论 UAC 是否确认、服务是否停得下来，托盘都必须能退出，绝不永久挂起。
        """
        # 「正在退出」拦截：防止退出清理中再次调用 start_service/stop_service/on_exit（日志里 30s stopping 等待期间用户又点了启动服务）
        with self._exit_lock:
            if self._exit_in_progress:
                logger.info("on_exit: 退出清理已在进行中，忽略重复调用")
                try:
                    show_notify("正在退出，请稍候…", APP_NAME)
                except Exception:
                    pass
                return
            self._exit_in_progress = True

        logger.info("on_exit: 开始退出清理")

        # 1. 停止后台轮询
        logger.info("on_exit 清理步骤[1/7]: 停止后台轮询线程（调用 stop_background_threads）")  # 为什么打这条日志：追踪清理步骤顺序
        self.stop_background_threads()

        # 2. 等待登录线程结束（这些线程可能会调 show_* 弹通知，所以放在 GUI 存活期内）
        logger.info("on_exit 清理步骤[2/7]: 等待登录线程结束（轮询线程 join 2s）")  # 为什么打这条日志：追踪清理步骤顺序
        try:
            for t in gui_thread.get_login_threads():
                t.join(timeout=2.0)
        except Exception:
            logger.exception("on_exit: 等待登录线程异常")

        # 3. 停服：elevated_service_control 会阻塞在 UAC 确认框，
        #    故放入独立线程并 join(15) 防护；超时/UAC 拒绝均不阻塞退出。
        logger.info("on_exit 清理步骤[3/7]: 停止服务（停服+轮询确认，带超时防护）")  # 为什么打这条日志：追踪清理步骤顺序
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

        # 4. 进程清理（互斥锁延后到 icon.stop() 之后释放，见步骤 7）
        logger.info("on_exit 清理步骤[4/7]: 清理 sau 进程（kill_sau_processes）")  # 为什么打这条日志：追踪清理步骤顺序
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

        # 5. 停止 GUI 线程（所有可能弹对话框/通知的业务逻辑必须已经结束）
        logger.info("on_exit 清理步骤[5/7]: 停止 GUI 线程（调用 gui_thread.stop）")  # 为什么打这条日志：追踪清理步骤顺序
        try:
            gui_thread.stop()
        except Exception:
            logger.exception("on_exit: 停止 GUI 线程异常")

        # 6. 停止托盘图标（停止图标后 icon.notify() 就不能用了，所以 GUI 要先停完）
        try:
            logger.info("on_exit 清理步骤[6/7]: 执行 icon.stop() 停止托盘图标")  # 为什么打这条日志：确认托盘停止步骤已执行
            icon.stop()
        except Exception:
            logger.exception("on_exit: 停止托盘图标异常")

        # 7. 释放单实例互斥锁（尽量靠后：icon.stop() 异常时避免无锁窗口
        #    导致用户重启出双实例）
        logger.info("on_exit 清理步骤[7/7]: 释放单实例互斥锁 release_single_instance")  # 为什么打这条日志：追踪清理步骤顺序
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
        """后台线程：每 3s 轮询本地 API /status，同一循环顺带 GET /upgrade。

        为什么轮询失败时增加 SCM 状态兜底：
          - 开发环境下通常用 `python sau_backend.py` 启动进程（本地 5410 端口起得很快）
          - 打包为 Windows 服务后：
              1. Session 0 下启动慢（本地 API 服务器 aiohttp 启动会晚于托盘启动 5~10s）
              2. 即使 SCM 报告服务 running，Local API 可能仍在初始化（init_db / generate_local_token）
              3. generate_local_token 失败 → 500 auth fail → 轮询一直报通
              4. 端口冲突 / 绑定失败 → 5410 不通，但 SCM 显示 running
          - 简单把 service_running 设成 False 会让用户看到"服务未运行"的红叉，但实际上 SCM 已经
            在正常运行，只是本地 API 还没 ready。
          - 解决：轮询失败时"相信 Windows SCM 的 running/starting 状态"作为
            service_running 的兜底，真实反映「服务已启动但本地 API 还没起来」的状态，
            同时把轮询失败的具体原因（timeout/401/500/refused）打到 warning 日志，便于排障。
        """
        # 记录上一次的轮询失败类型，用于去重日志（相同类型不连续打满）
        last_fail_tag: str | None = None
        # 记录上一次的 WS 连接状态，用于边沿跳变检测（False→True 时弹 Toast）
        # 为什么放在这里而不是状态字典里：
        #   state.set_status/get_status 存的是「当前状态」用于 UI 展示，
        #   「上次状态」是轮询线程内部的临时比对变量，只用于触发一次性通知，
        #   不需要对外暴露，也不需要线程间共享（轮询只有一个线程）。
        last_ws_connected: bool = False

        while not stop_event.is_set():
            # ------------------------------------------------------------------
            # 先 GET /upgrade（顺便拿升级状态，失败用缓存）
            # ------------------------------------------------------------------
            try:
                fetched = _fetch_local_api("/upgrade")
                upgrade_state = fetched if isinstance(fetched, dict) else {}
            except Exception:
                upgrade_state = self.get_upgrade_state()
            with self._upgrade_state_lock:
                self._current_upgrade_state = upgrade_state
            self._track_applying(upgrade_state)

            # ------------------------------------------------------------------
            # GET /status 拿核心状态
            # ------------------------------------------------------------------
            fail_tag: str | None = None
            scm_running: bool | None = None
            # 本次循环拿到的 WS 连接状态（用于最后统一更新 last_ws_connected）
            current_ws_connected: bool = False

            try:
                data = _fetch_local_api("/status")
                data["service_running"] = True
                data["updating"] = False
                state.set_status(data)
                self._check_apply_result(data)
                # 本地 API 通了，WS 状态直接用服务端返回值
                current_ws_connected = bool(data.get("ws_connected", False))
            except Exception as e:
                # 细化诊断：区分 timeout / refused / 401 token 错 / 500 token 未生成 / 其他
                # 为什么：打包后"一直未连接"的用户场景里，最常见的根因 90% 是下面 4 种之一，
                # 日志里直接写明后用户不用去猜"到底是服务没启动，还是 token 错，还是端口被占"
                msg = str(e).lower()
                if isinstance(e, TimeoutError) or "timeout" in msg or "timed out" in msg:
                    fail_tag = "HTTP_TIMEOUT_2s"
                    warn_text = (
                        "status 轮询超时(2s)，可能是服务 Session 0 初始化慢、或网络事件循环阻塞"
                    )
                elif "refused" in msg or "winerror 1225" in msg or "connection reset" in msg:
                    fail_tag = "CONN_REFUSED_5410"
                    warn_text = (
                        "本地 API 127.0.0.1:5410 连接被拒绝，可能是："
                        "① 服务还没真正起来 ② 5410 被其他程序占用 ③ LocalApiServer(aiohttp) 未启动成功"
                    )
                elif "401" in msg or "unauthorized" in msg:
                    fail_tag = "TOKEN_401_MISMATCH"
                    warn_text = (
                        "本地 API 鉴权失败(401)：托盘读的 local_token 与服务生成的 token 不一致，"
                        "可能服务重装后 credential.bin 未同步"
                    )
                elif "500" in msg or "local token not configured" in msg:
                    fail_tag = "TOKEN_500_MISSING"
                    warn_text = (
                        "本地 API 返回 500(Local token missing)："
                        "服务 generate_local_token() 未生成成功，"
                        "检查 SAU_HOME 目录权限（需允许 SYSTEM 写 local_token.bin）"
                    )
                else:
                    fail_tag = f"STATUS_FETCH_ERR_{type(e).__name__}"
                    warn_text = f"status 轮询未知异常: {e}"

                # [SCM 兜底] 轮询失败时不要直接把 service_running=False，先查 SCM
                # 为什么：打包成 Windows 服务后，服务刚启动的几秒里 SCM 显示 running，
                # 但本地 API 还没 ready，直接报 False 会让用户错觉"服务未启动"
                try:
                    from sau_service.service_host import get_service_status
                    scm_status = get_service_status()
                    if scm_status in ("running", "starting"):
                        scm_running = True
                    elif scm_status in ("stopping", "stopped", "paused", "pausing"):
                        scm_running = False
                    else:
                        scm_running = None  # not installed / unknown → 不兜底
                except Exception as sce:
                    fail_tag_suffix = f"+SCM_QUERY_ERR_{type(sce).__name__}"
                    fail_tag = (fail_tag or "") + fail_tag_suffix if fail_tag else fail_tag_suffix[1:]
                    scm_running = None

                # service_running 优先级：SCM running/starting → True；SCM stopped → False；其他按 None
                if scm_running is True:
                    # 兜底：服务在 SCM 层面是 running，只是本地 API 还没 ready
                    fallback_service_running = True
                elif scm_running is False:
                    fallback_service_running = False
                else:
                    # SCM 查询失败 / 未安装：保守认为服务未运行
                    fallback_service_running = False

                # 本地 API 没通 → WS 肯定未连接（current_ws_connected 保持 False 默认值）
                state.set_status({
                    "service_running": fallback_service_running,
                    "ws_connected": False,
                    "clock_sync_status": "unknown",
                    "updating": self.is_applying(upgrade_state),
                    # SCM 兜底状态备注：如果轮询失败但 SCM running，就把失败信息记到 debug，
                    # 不影响托盘图标颜色（图标按 fallback_service_running 着色）
                })

                # 去重日志：同一种失败类型 3s 一轮没必要打满 info，用 warning 不重复打
                if fail_tag != last_fail_tag:
                    scm_hint = (
                        f"（SCM 兜底：service_running={fallback_service_running}，"
                        f"SCM_status={getattr(self, '_last_scm_status', 'N/A')}）"
                    )
                    logger.warning(
                        "_poll_status_loop: %s fail_tag=%s %s",
                        warn_text, fail_tag, scm_hint,
                    )
                    # 记录最近一次 SCM 状态（下次日志里能看到）
                    try:
                        from sau_service.service_host import get_service_status as _gs  # noqa: F811
                        self._last_scm_status = _gs()
                    except Exception:
                        self._last_scm_status = "query_failed"
            finally:
                last_fail_tag = fail_tag

            # ── WS 连接边沿跳变检测（上一轮 False → 本轮 True → 弹 Toast）──
            # 为什么不用 on_connection_change 回调放在服务端：
            #   服务进程运行在 Session 0（LocalSystem），Windows 禁止 Session 0
            #   直接弹 Toast/MessageBox（Session 0 Isolation）；托盘在用户
            #   Session 中，由托盘轮询触发通知才能真正显示。
            # 为什么是「上升沿」检测而不是每次 True 都弹：
            #   轮询间隔 3s，如果连上后每次都弹，用户会被连续 Toast 刷屏；
            #   只在从 False 跳变到 True 的第一次弹一次，符合「首次连接成功」的语义。
            if current_ws_connected and not last_ws_connected:
                show_notify("连接服务器成功")

            last_ws_connected = current_ws_connected

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
        """轮询等待服务达到目标状态。

        修复点（问题 4：stop 卡 stopping 30s 超时）：
            当目标是 stopped 且轮询期间连续处于 stopping 超过 25s、
            或目标是 running 且连续处于 starting 超过 25s 时，
            认为服务宿主进程自己卡了（典型是 asyncio 事件循环关不回来），
            调用 _force_kill_sau_service 杀掉 sau-service.exe —— 进程消失后
            SCM 会立刻把状态置为 stopped / stopped-start 失败，从而避免 30s
            轮询一直停在中间状态最后抛 TimeoutError。
        """
        from sau_service.service_host import get_service_status
        deadline = time.time() + timeout
        # 记录连续停在 stopping / starting 的起算时刻
        pending_since: float | None = None
        pending_tag: str | None = None
        while time.time() < deadline:
            current = get_service_status()
            if current == target_status:
                return
            # 识别 stopping / starting 的长时间停顿：连续同一状态超过 25s
            if target_status == "stopped" and current == "stopping":
                if pending_tag == "stopping":
                    if (time.time() - pending_since) >= 25:
                        ServiceController._force_kill_sau_service("_wait_for_status: stopping>25s")
                        pending_tag = None
                else:
                    pending_since = time.time()
                    pending_tag = "stopping"
            elif target_status == "running" and current == "starting":
                if pending_tag == "starting":
                    if (time.time() - pending_since) >= 25:
                        ServiceController._force_kill_sau_service("_wait_for_status: starting>25s")
                        pending_tag = None
                else:
                    pending_since = time.time()
                    pending_tag = "starting"
            else:
                pending_tag = None
            time.sleep(1)
        raise TimeoutError(
            f"服务未能在 {timeout}s 内达到 {target_status} 状态"
            f"（当前: {get_service_status()}）"
        )
