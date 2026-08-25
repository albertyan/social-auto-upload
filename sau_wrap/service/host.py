# -*- coding: utf-8 -*-
"""服务宿主：``sau agent`` 的 pywin32 服务化封装（设计文档 §4.1）。

要点（第 4 章定案）：
- 走 ``win32serviceutil`` 标准路径，不自研 CreateService（§4.1、§9.5 教训 4）；
- 服务名固定 ``SAUAgentService``，显示名/描述见类属性；
- 支持 pywin32 服务宿主惯例参数：``--startup auto``（SCM 拉起时自动附加）、
  以及标准动词 ``debug``（前台调试）/ ``install`` / ``remove`` 等；
- SCM 拉起时 ImagePath 为 ``"<exe 或 python>" agent --startup auto``（§3.2 / §4.1）；
- 本步服务主逻辑仅启动 agent 空跑骨架（写日志、保持运行、响应停止）。

日志落 ``%ProgramData%\\SAU\\logs\\service.log``（§3.6 / §14.1）。
"""

from __future__ import annotations

import os
import sys

import servicemanager
import win32event
import win32service
import win32serviceutil

from sau_wrap.agent.core import AgentSkeleton
from sau_wrap.logutil import get_service_logger
from sau_wrap.version import APP_VERSION


class SAUAgentService(win32serviceutil.ServiceFramework):
    """SAUAgentService —— SAU Agent 服务（本步为空跑骨架）。"""

    #: 服务名（注册键名，设计文档 §4.1）
    _svc_name_ = "SAUAgentService"
    #: 显示名
    _svc_display_name_ = "SAU Agent Service"
    #: 服务描述（注册后写入，便于 services.msc 辨识）
    _svc_description_ = (
        "SAU 包装层 Agent 服务：WS 主循环 + 5409 本地 API。"
        "当前为任务 #12 第一步空跑骨架，不连接 opcgeo。"
    )

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        #: SCM 停止信号事件
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):
        """SCM 请求停止：先报 STOP_PENDING，再置位停止事件。"""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        """服务主逻辑：初始化日志 → 运行 agent 骨架 → 等待停止。"""
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
            AgentSkeleton(logger).run(self.stop_event)
        except Exception:
            logger.exception("服务主逻辑异常退出")
            try:
                servicemanager.LogErrorMsg("SAUAgentService 主逻辑异常")
            except Exception:  # pragma: no cover
                pass
        logger.info("服务停止: name=%s", self._svc_name_)
        try:
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STOPPED,
                (self._svc_name_, ""),
            )
        except Exception:  # pragma: no cover
            pass


def run_foreground() -> None:
    """前台自测模式（``agent run-fg``）：不依赖服务注册，直接运行服务主逻辑。

    说明：pywin32 标准 ``debug`` 动词要求服务已安装；本动词用于未注册服务时
    验证启动/停止逻辑（Ctrl+C 退出）。日志写 ``service.log`` 并同时输出控制台。
    """
    import signal

    logger = get_service_logger(also_console=True)
    stop_event = win32event.CreateEvent(None, 0, 0, None)

    def _on_signal(signum, frame):  # noqa: ANN001
        logger.info("收到信号 %s，置位停止事件", signum)
        win32event.SetEvent(stop_event)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    if hasattr(signal, "SIGBREAK"):  # Windows：Ctrl+Break / CTRL_BREAK_EVENT
        signal.signal(signal.SIGBREAK, _on_signal)
    logger.info("前台自测模式启动（run-fg，非 SCM 管理）")
    try:
        AgentSkeleton(logger).run(stop_event)
    except Exception:
        logger.exception("前台自测模式异常退出")
        raise
    logger.info("前台自测模式退出")


def main(argv=None):
    """``agent`` 子命令入口：委托 pywin32 标准命令行处理。

    支持的动词（win32serviceutil 标准）：
    - ``run-fg``   前台自测（不依赖服务注册，本步验证启动/停止逻辑的主要方式）
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
