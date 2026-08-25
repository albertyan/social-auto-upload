# -*- coding: utf-8 -*-
"""服务宿主：``sau agent`` 的 pywin32 服务化封装（设计文档 §4.1）。

要点（第 4 章定案）：
- 走 ``win32serviceutil`` 标准路径，不自研 CreateService（§4.1、§9.5 教训 4）；
- 服务名固定 ``SAUAgentService``，显示名/描述见类属性；
- 支持 pywin32 服务宿主惯例参数：``--startup auto``（SCM 拉起时自动附加）、
  以及标准动词 ``debug``（前台调试）/ ``install`` / ``remove`` 等；
- SCM 拉起时 ImagePath 为 ``"<exe 或 python>" agent --startup auto``（§3.2 / §4.1）；
- 服务主逻辑（S2 起）：以 ``WindowsSelectorEventLoopPolicy`` 驱动
  :class:`sau_wrap.agent.ws_client.WSClient` WS 主循环（§5.1 规避 Session 0
  Proactor 管道竞态）；
- **异常语义**（review 修复，§4.2）：``SvcDoRun`` 捕获主逻辑异常后记录日志并
  **重新抛出**，不得干净返回——否则 SCM 视为正常停止，失败重启策略失效。

日志落 ``%ProgramData%\\SAU\\logs\\service.log``（§3.6 / §14.1）。
"""

from __future__ import annotations

import asyncio
import os
import sys

import servicemanager
import win32event
import win32service
import win32serviceutil

from sau_wrap.logutil import get_service_logger
from sau_wrap.version import APP_VERSION


def _run_agent_blocking(logger, request_async_stop) -> None:
    """以 asyncio 驱动 WS 主循环（阻塞直至停止）。

    - ``WindowsSelectorEventLoopPolicy``（§5.1）；
    - ``request_async_stop`` 返回一个回调注册器，供外部（服务停止/信号）置位
      asyncio stop 事件——签名：``register(callback)``，callback 无参。
    """
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        stop_event = asyncio.Event()
        resume_event = asyncio.Event()
        request_async_stop(lambda: loop.call_soon_threadsafe(stop_event.set))

        from sau_wrap.agent.ws_client import WSClient

        client = WSClient(logger, stop_event, resume_event)
        loop.run_until_complete(client.run())
    finally:
        try:
            loop.close()
        except Exception:  # pragma: no cover
            pass


class SAUAgentService(win32serviceutil.ServiceFramework):
    """SAUAgentService —— SAU Agent 服务（S2：WS 主循环）。"""

    #: 服务名（注册键名，设计文档 §4.1）
    _svc_name_ = "SAUAgentService"
    #: 显示名
    _svc_display_name_ = "SAU Agent Service"
    #: 服务描述（注册后写入，便于 services.msc 辨识）
    _svc_description_ = (
        "SAU 包装层 Agent 服务：WS 主循环 + 5409 本地 API。"
        "当前为 S2：Agent WS 核心（注册/心跳/重连退避/任务落库/结果补发）。"
    )

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        #: SCM 停止信号事件
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        #: asyncio 停止回调（SvcStop 线程 → 事件循环线程置位 stop 事件）
        self._async_stop_callback = None

    def SvcStop(self):
        """SCM 请求停止：先报 STOP_PENDING，再置位停止事件（含 asyncio 侧）。"""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)
        if self._async_stop_callback is not None:
            try:
                self._async_stop_callback()
            except Exception:  # pragma: no cover
                pass

    def SvcDoRun(self):
        """服务主逻辑：初始化日志 → 运行 WS 主循环 → 等待停止。

        异常处理（review 修复）：主逻辑异常记录日志后**重新抛出**，
        令 SCM 以失败计数，触发 §4.2 梯度重启策略；干净返回会使重启失效。
        """
        # 前台调试（debug 动词）时同时输出到控制台，便于验证
        is_debug = "--debug-console" in sys.argv
        logger = get_service_logger(also_console=is_debug)
        try:
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
        except Exception:  # pragma: no cover - 事件日志写入失败不影响主流程
            pass

        logger.info(
            "服务启动: name=%s version=%s pid=%s cwd=%s",
            self._svc_name_,
            APP_VERSION,
            os.getpid(),
            os.getcwd(),
        )
        try:
            def _register_stop(cb):
                self._async_stop_callback = cb

            _run_agent_blocking(logger, _register_stop)
            logger.info("服务停止: name=%s", self._svc_name_)
            try:
                servicemanager.LogMsg(
                    servicemanager.EVENTLOG_INFORMATION_TYPE,
                    servicemanager.PYS_SERVICE_STOPPED,
                    (self._svc_name_, ""),
                )
            except Exception:  # pragma: no cover
                pass
        except Exception:
            logger.exception("服务主逻辑异常退出（将以失败态结束，触发 SCM 失败重启策略）")
            try:
                servicemanager.LogErrorMsg("SAUAgentService 主逻辑异常")
            except Exception:  # pragma: no cover
                pass
            raise  # 不得干净返回：令 SCM 计为失败 → §4.2 梯度重启生效


def run_foreground() -> None:
    """前台自测模式（``agent run-fg``）：不依赖服务注册，直接运行服务主逻辑。

    说明：pywin32 标准 ``debug`` 动词要求服务已安装；本动词用于未注册服务时
    验证启动/停止逻辑（Ctrl+C 退出）。日志写 ``service.log`` 并同时输出控制台。
    """
    import signal

    logger = get_service_logger(also_console=True)
    logger.info("前台自测模式启动（run-fg，非 SCM 管理）")

    stop_callbacks: list = []

    def _register_stop(cb):
        stop_callbacks.append(cb)

    def _on_signal(signum, frame):  # noqa: ANN001
        logger.info("收到信号 %s，置位停止事件", signum)
        for cb in stop_callbacks:
            try:
                cb()
            except Exception:  # pragma: no cover
                pass

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    if hasattr(signal, "SIGBREAK"):  # Windows：Ctrl+Break / CTRL_BREAK_EVENT
        signal.signal(signal.SIGBREAK, _on_signal)
    try:
        _run_agent_blocking(logger, _register_stop)
    except Exception:
        logger.exception("前台自测模式异常退出")
        raise
    logger.info("前台自测模式退出")


def main(argv=None):
    """``agent`` 子命令入口：委托 pywin32 标准命令行处理。

    支持的动词（win32serviceutil 标准）：
    - ``run-fg``   前台自测（不依赖服务注册，验证启动/停止逻辑的主要方式）
    - ``debug``    前台调试运行（要求服务已安装）
    - ``install``  安装服务（等价 ``sau service install``，但不含延迟自启加固）
    - ``remove``   移除服务
    - ``start``/``stop`` 启停
    - ``--startup auto`` 由 SCM 拉起时自动附加，进入服务主循环
    """
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "run-fg":
        run_foreground()
        return
    if "--startup" in argv:
        # SCM 拉起（服务上下文）：直接进入服务控制分发器（pywin32 冻结 exe 标准形态：
        # PrepareToHostSingle 登记服务类后，无参调用 StartServiceCtrlDispatcher）
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(SAUAgentService)
        servicemanager.StartServiceCtrlDispatcher()
        return
    # win32serviceutil.HandleCommandLine 约定：argv[1:] 才是参数段
    win32serviceutil.HandleCommandLine(SAUAgentService, argv=["agent"] + list(argv))


if __name__ == "__main__":  # pragma: no cover
    main()
