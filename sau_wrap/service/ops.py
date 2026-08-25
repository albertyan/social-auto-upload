# -*- coding: utf-8 -*-
"""服务生命周期管理：``service install|remove|start|stop|status|upgrade``。

设计文档第 4 章定案（方案 C 加固）：
- install 走 ``win32serviceutil.InstallService`` 标准路径（§4.1），不自研 CreateService；
- 注册后立即配置**延迟自启**（SERVICE_CONFIG_DELAYED_AUTO_START_INFO，§4.2）；
- 注册后立即配置**失败重启策略**（梯度重启：30s/60s/120s，24h 重置计数，§4.2）；
- ImagePath 固定形态：``"<exe>" agent --startup auto``（§3.2 / §4.1）；
  源码开发形态等价：``"<python>" "<repo>\\sau_wrap\\__main__.py" agent --startup auto``；
- start 后轮询状态，窗口放宽至 60 秒（§4.2：兼容 Session 0 冷启动）。

需要管理员权限；权限不足时各命令输出明确错误提示（禁止静默失败，§17 核心原则）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import click
import win32service
import win32serviceutil

SERVICE_NAME = "SAUAgentService"
DISPLAY_NAME = "SAU Agent Service"
DESCRIPTION = (
    "SAU 包装层 Agent 服务：WS 主循环 + 5409 本地 API"
    "（设计文档 §3.2/§4.1；当前为空跑骨架原型）"
)

#: 服务状态码 → 可读文本
_STATE_TEXT = {
    win32service.SERVICE_STOPPED: "已停止 (STOPPED)",
    win32service.SERVICE_START_PENDING: "正在启动 (START_PENDING)",
    win32service.SERVICE_STOP_PENDING: "正在停止 (STOP_PENDING)",
    win32service.SERVICE_RUNNING: "运行中 (RUNNING)",
    win32service.SERVICE_CONTINUE_PENDING: "正在继续 (CONTINUE_PENDING)",
    win32service.SERVICE_PAUSE_PENDING: "正在暂停 (PAUSE_PENDING)",
    win32service.SERVICE_PAUSED: "已暂停 (PAUSED)",
}

#: 失败重启策略（§4.2）：首次失败 30 秒后重启，梯度加长，24 小时重置计数
_FAILURE_RESET_SECONDS = 86400
_FAILURE_ACTIONS_MS = (30000, 60000, 120000)

#: start 后轮询窗口（§4.2：放宽至 30~60 秒）
START_WAIT_SECONDS = 60
STOP_WAIT_SECONDS = 30


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def service_image_parts() -> tuple[str, str]:
    """计算 SCM ImagePath 的 (可执行文件, 参数) 二元组。"""
    if getattr(sys, "frozen", False):
        # 打包形态：ImagePath = "<{app}\sau.exe>" agent --startup auto（§3.2）
        return sys.executable, "agent --startup auto"
    # 源码开发形态：由 __main__.py 的 sys.path 引导保证任意工作目录可导入
    entry_py = _repo_root() / "sau_wrap" / "__main__.py"
    return sys.executable, f'"{entry_py}" agent --startup auto'


def service_image_path() -> str:
    exe, args = service_image_parts()
    return f'"{exe}" {args}'


def _is_admin_error(exc: win32service.error) -> bool:
    return exc.winerror in (5, 1314)  # ACCESS_DENIED / NOT_ALL_ASSIGNED


def _open_service(name: str, access: int):
    hscm = win32service.OpenSCManager(None, None, access)
    try:
        return win32service.OpenService(hscm, name, access)
    finally:
        win32service.CloseServiceHandle(hscm)


def get_state() -> int | None:
    """返回服务当前状态码；服务不存在返回 None。"""
    try:
        hs = _open_service(SERVICE_NAME, win32service.SERVICE_QUERY_STATUS)
    except win32service.error:
        return None
    try:
        return win32service.QueryServiceStatus(hs)[1]
    finally:
        win32service.CloseServiceHandle(hs)


def get_image_path() -> str:
    try:
        hs = _open_service(SERVICE_NAME, win32service.SERVICE_QUERY_CONFIG)
    except win32service.error:
        return ""
    try:
        # QueryServiceConfig 返回元组：索引 3 为 ImagePath（lpBinaryPathName）
        return win32service.QueryServiceConfig(hs)[3]
    finally:
        win32service.CloseServiceHandle(hs)


def _sc(args: list[str]) -> bool:
    """调用 sc.exe 作为加固项的回退路径（§4.2 原文即 `sc config ...`）。"""
    import subprocess

    result = subprocess.run(["sc.exe"] + args, capture_output=True, text=True)
    if result.returncode != 0:
        click.echo(
            f"[警告] sc.exe {' '.join(args)} 失败: "
            f"{(result.stderr or result.stdout).strip()}",
            err=True,
        )
        return False
    return True


def _apply_reliability_policy() -> list[str]:
    """注册后立即配置：延迟自启 + 失败梯度重启（§4.2）。返回未生效项列表。"""
    problems: list[str] = []
    # 1) 延迟自启（DELAYED_AUTO_START），避开开机启动风暴
    try:
        hs = _open_service(SERVICE_NAME, win32service.SERVICE_CHANGE_CONFIG)
        try:
            win32service.ChangeServiceConfig2(
                hs, win32service.SERVICE_CONFIG_DELAYED_AUTO_START_INFO, 1
            )
        finally:
            win32service.CloseServiceHandle(hs)
    except win32service.error as exc:
        click.echo(f"[警告] 延迟自启 API 设置失败（{exc}），回退 sc.exe ...", err=True)
        if not _sc(["config", SERVICE_NAME, "start=", "delayed-auto"]):
            problems.append("延迟自启")
    # 2) 失败重启：restart/30000/restart/60000/restart/120000 failure= reset/86400
    try:
        hs = _open_service(SERVICE_NAME, win32service.SERVICE_CHANGE_CONFIG)
        try:
            failure_actions = {
                "ResetPeriod": _FAILURE_RESET_SECONDS,
                "RebootMsg": "",
                "Command": "",
                "Actions": [
                    (win32service.SC_ACTION_RESTART, delay)
                    for delay in _FAILURE_ACTIONS_MS
                ],
            }
            win32service.ChangeServiceConfig2(
                hs, win32service.SERVICE_CONFIG_FAILURE_ACTIONS, failure_actions
            )
        finally:
            win32service.CloseServiceHandle(hs)
    except win32service.error as exc:
        click.echo(f"[警告] 失败重启策略 API 设置失败（{exc}），回退 sc.exe ...", err=True)
        if not _sc(
            [
                "failure",
                SERVICE_NAME,
                f"reset= {_FAILURE_RESET_SECONDS}",
                "actions= "
                + "/".join(f"restart/{d}" for d in _FAILURE_ACTIONS_MS),
            ]
        ):
            problems.append("失败重启策略")
    return problems


def cmd_install() -> None:
    """注册服务并立即应用延迟自启 + 失败重启策略（§4.2）。"""
    exe, args = service_image_parts()
    class_string = "sau_wrap.service.host.SAUAgentService"
    click.echo(f"注册服务 {SERVICE_NAME} ...")
    click.echo(f"ImagePath: \"{exe}\" {args}")
    try:
        win32serviceutil.InstallService(
            class_string,
            SERVICE_NAME,
            DISPLAY_NAME,
            startType=win32service.SERVICE_AUTO_START,
            exeName=exe,
            exeArgs=args,
            description=DESCRIPTION,
        )
    except win32service.error as exc:
        if _is_admin_error(exc):
            click.echo(
                f"[错误] 权限不足，无法注册服务（{exc}）。"
                "请以管理员身份运行后重试。",
                err=True,
            )
            sys.exit(1)
        raise
    click.echo("服务注册成功。")
    problems = _apply_reliability_policy()
    if problems:
        click.echo(
            f"[警告] 以下加固项未生效：{'、'.join(problems)}。"
            "可稍后以管理员身份重新执行 service install 或手工 sc config。",
            err=True,
        )
    else:
        click.echo("可靠性策略已应用：启动类型=自动(延迟启动)；失败重启=30/60/120 秒梯度，24 小时重置计数。")
    click.echo("下一步可执行: service start（随后用 service status 查看）")


def cmd_remove() -> None:
    """停止（如运行中）并移除服务注册。"""
    try:
        state = get_state()
        if state is None:
            click.echo(f"服务 {SERVICE_NAME} 不存在，无需移除。")
            return
        if state not in (win32service.SERVICE_STOPPED,):
            click.echo("服务正在运行，先停止 ...")
            win32serviceutil.StopServiceWithDeps(SERVICE_NAME, waitSecs=STOP_WAIT_SECONDS)
        win32serviceutil.RemoveService(SERVICE_NAME)
        click.echo(f"服务 {SERVICE_NAME} 已移除。")
    except win32service.error as exc:
        if _is_admin_error(exc):
            click.echo(
                f"[错误] 权限不足，无法移除服务（{exc}）。请以管理员身份运行后重试。",
                err=True,
            )
            sys.exit(1)
        raise


def cmd_start() -> None:
    """启动服务并轮询至 RUNNING（窗口 60 秒，§4.2）。"""
    if get_state() is None:
        click.echo(f"[错误] 服务 {SERVICE_NAME} 未安装，请先执行 service install。", err=True)
        sys.exit(1)
    try:
        win32serviceutil.StartService(SERVICE_NAME, None)
    except win32service.error as exc:
        if _is_admin_error(exc):
            click.echo(
                f"[错误] 权限不足，无法启动服务（{exc}）。请以管理员身份运行后重试。",
                err=True,
            )
            sys.exit(1)
        click.echo(f"[错误] 启动失败：{exc}", err=True)
        sys.exit(1)
    deadline = time.time() + START_WAIT_SECONDS
    while time.time() < deadline:
        state = get_state()
        if state == win32service.SERVICE_RUNNING:
            click.echo(f"服务 {SERVICE_NAME} 已运行 (RUNNING)。")
            return
        if state == win32service.SERVICE_STOPPED:
            click.echo(
                "[错误] 服务启动后回到 STOPPED，请查看 %ProgramData%\\SAU\\logs\\service.log "
                "与 Windows 事件日志。",
                err=True,
            )
            sys.exit(1)
        time.sleep(1)
    click.echo(
        f"[警告] {START_WAIT_SECONDS} 秒内未观测到 RUNNING（当前状态: "
        f"{_STATE_TEXT.get(get_state(), get_state())}）。",
        err=True,
    )
    sys.exit(1)


def cmd_stop() -> None:
    """停止服务（等待至多 30 秒，§13.1）。"""
    if get_state() is None:
        click.echo(f"[错误] 服务 {SERVICE_NAME} 未安装。", err=True)
        sys.exit(1)
    try:
        win32serviceutil.StopServiceWithDeps(SERVICE_NAME, waitSecs=STOP_WAIT_SECONDS)
        click.echo(f"服务 {SERVICE_NAME} 已停止。")
    except win32service.error as exc:
        if _is_admin_error(exc):
            click.echo(
                f"[错误] 权限不足，无法停止服务（{exc}）。请以管理员身份运行后重试。",
                err=True,
            )
            sys.exit(1)
        raise


def cmd_status() -> None:
    """查询服务状态与 ImagePath。"""
    state = get_state()
    if state is None:
        click.echo(f"服务 {SERVICE_NAME}: 未安装")
        sys.exit(1)
    click.echo(f"服务 {SERVICE_NAME}: {_STATE_TEXT.get(state, f'未知({state})')}")
    click.echo(f"ImagePath: {get_image_path()}")


def cmd_upgrade() -> None:
    """【占位】实施计划 S7 实现。

    届时职责：配合服务进程（SYSTEM）升级编排（§7.4 十步链路）——
    runner 副本、备份、静默安装、回滚。
    """
    click.echo("service upgrade 尚未实现（实施计划 S7：升级编排，见设计文档 §7.4）。")
