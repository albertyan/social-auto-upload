"""
sau_service.service_host
~~~~~~~~~~~~~~~~~~~~~~~~
pywin32 Windows 服务宿主。

将 SAUAgentCore（WS 长连接）与 LocalApiServer（本地控制 API）作为
Windows 系统服务运行在 Session 0（Local System 账户）。

服务名：SAUAgentService
显示名：SAU Publish Agent
启动类型：自动（延迟启动）
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ---------------------------------------------------------------------------
# 全局崩溃捕获（最早注册，捕获模块级 import 阶段的崩溃）
# ---------------------------------------------------------------------------
def _crash_log_write(msg: str) -> None:
    """向 exe 所在目录写崩溃日志（仅依赖 sys，不依赖任何项目变量）。"""
    try:
        import datetime
        import traceback as _tb_mod
        exe = sys.executable if sys.executable.lower().endswith(".exe") else None
        if exe:
            log_path = Path(exe).resolve().parent / "sau-service-crash.log"
        else:
            log_path = Path(os.environ.get("TEMP", ".")) / "sau-service-crash.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass  # 极端情况放弃


def _global_except_hook(exc_type, exc_value, exc_tb):
    """全局未捕获异常钩子 → 写崩溃日志后调用默认处理器。"""
    import traceback
    _crash_log_write("=== UNCAUGHT EXCEPTION (module-level) ===")
    for line in traceback.format_exception(exc_type, exc_value, exc_tb):
        _crash_log_write(line.rstrip())
    # 同时输出到 stderr（开发环境可见）
    traceback.print_exception(exc_type, exc_value, exc_tb)


sys.excepthook = _global_except_hook

# ---------------------------------------------------------------------------
# 路径修正：确保项目根目录在 sys.path（pywin32 服务宿主可能从任意 cwd 启动）
# ---------------------------------------------------------------------------
# 扁平化安装后，service_host.py 直接位于 SAU 安装目录（D:\Program Files\SAU\）
# Nuitka standalone: __file__ 可能未定义，依次回退 sys.argv[0] / sys.executable
def _resolve_project_root() -> str:
    """解析项目根目录（SAU 安装目录）。"""
    try:
        # 脚本模式：__file__ = D:\Program Files\SAU\service_host.py → .parent = SAU 目录
        return str(Path(__file__).resolve().parent)
    except NameError:
        pass
    # Nuitka standalone：__file__ 不存在，用 sys.argv[0]（实际 exe 路径）
    if sys.argv and sys.argv[0].lower().endswith(".exe"):
        return str(Path(sys.argv[0]).resolve().parent)
    # 最终回退：sys.executable（Nuitka 下可能指向 python.exe）
    if sys.executable:
        return str(Path(sys.executable).resolve().parent)
    return "."

_PROJECT_ROOT = _resolve_project_root()
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 垫片：在一切 import sau_cli 前改写 conf.BASE_DIR
from sau_tray.home_shim import SAU_HOME, apply_home_shim

apply_home_shim()

# ---------------------------------------------------------------------------
# 日志初始化（服务启动最早期）
# ---------------------------------------------------------------------------
_LOG_FILE = SAU_HOME / "logs" / "sau-service.log"
_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def _setup_logging() -> None:
    """初始化服务日志：文件（轮转 10MB×5）+ Windows 事件日志。"""
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # 文件 handler（轮转）
    fh = RotatingFileHandler(
        str(_LOG_FILE),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(fh)

    # Windows 事件日志（可选，pywin32 环境下可用）
    try:
        import servicemanager  # type: ignore[import-untyped]

        class _WinEventHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                try:
                    msg = self.format(record)
                    if record.levelno >= logging.ERROR:
                        servicemanager.LogErrorMsg(f"[SAUAgentService] {msg}")
                    else:
                        servicemanager.LogInfoMsg(f"[SAUAgentService] {msg}")
                except Exception:
                    pass

        root.addHandler(_WinEventHandler())
    except ImportError:
        pass  # 非 pywin32 环境（开发机）忽略


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# pywin32 服务框架
# ---------------------------------------------------------------------------
try:
    import win32serviceutil  # type: ignore[import-untyped]
    import win32service  # type: ignore[import-untyped]
    import servicemanager  # type: ignore[import-untyped]
    _HAS_PYWIN32 = True
except ImportError:
    _HAS_PYWIN32 = False
    # 开发环境 stub，避免 import 时报错
    class _ServiceFrameworkStub:  # type: ignore[no-redef]
        _svc_name_ = ""
        _svc_display_name_ = ""
        def __init__(self, args=None): pass
        def ReportServiceStatus(self, *a, **kw): pass
    win32serviceutil = type(sys)("win32serviceutil")  # type: ignore[assignment]
    win32serviceutil.ServiceFramework = _ServiceFrameworkStub  # type: ignore[attr-defined]


if _HAS_PYWIN32:

    class SAUAgentService(win32serviceutil.ServiceFramework):  # type: ignore[misc, valid-type]
        """SAU Agent Windows 服务。"""

        _svc_name_ = "SAUAgentService"
        _svc_display_name_ = "SAU Publish Agent"
        _svc_description_ = (
            "SAU Publish Agent — 通过 WebSocket 连接 opcgeo 服务端，"
            "接收并执行社交媒体发布任务。"
        )

        def __init__(self, args: list[str] | None = None) -> None:
            super().__init__(args)
            self._stop_event: threading.Event | None = None
            self._asyncio_loop: asyncio.AbstractEventLoop | None = None

        # ------------------------------------------------------------------
        # SvcDoRun：服务主入口
        # ------------------------------------------------------------------
        def SvcDoRun(self) -> None:
            """服务启动入口：初始化日志 → 启动 asyncio 主循环。"""
            _setup_logging()
            logger.info("SAUAgentService starting (SAU_HOME=%s)", SAU_HOME)

            try:
                servicemanager.LogMsg(
                    servicemanager.EVENTLOG_INFORMATION_TYPE,
                    servicemanager.PYS_SERVICE_STARTED,
                    (self._svc_name_, ""),
                )
            except Exception:
                pass

            self._stop_event = threading.Event()

            # 初始化数据库
            from sau_agent_pkg.db_init import init_db
            try:
                db_path = init_db()
                logger.info("Database initialized at: %s", db_path)
            except Exception:
                logger.exception("Database initialization failed")

            # 生成 local_token（供托盘进程读取）
            from sau_agent_pkg.config import generate_local_token
            try:
                token = generate_local_token()
                logger.info("Local token generated")
            except Exception:
                logger.exception("Failed to generate local token")

            # 运行 asyncio 主循环
            try:
                asyncio.run(self._main(self._stop_event))
            except Exception:
                logger.exception("Service main loop crashed")
            finally:
                logger.info("SAUAgentService main loop exited cleanly")

        # ------------------------------------------------------------------
        # asyncio 主循环：agent + local_api 同一 loop
        # ------------------------------------------------------------------
        async def _main(self, stop_event: threading.Event) -> None:
            """同时运行 Agent 核心与本地 API 服务器。"""
            from sau_agent_pkg.config import load_config
            from sau_agent_pkg.core import SauAgentCore
            from sau_agent_pkg.local_api import LocalApiServer
            from sau_agent_pkg import updater

            config = load_config()
            agent = SauAgentCore(config=config)
            api = LocalApiServer(core=agent)

            # 半自动更新接线（M5）：upgrade_notice → 下载/校验/状态
            agent.on_upgrade_notice = updater.handle_upgrade_notice
            try:
                updater.cleanup_expired()
            except Exception:
                logger.exception("Upgrade cleanup failed")

            # 将 threading.Event 包装为 asyncio.Event 供内部使用
            async_stop = asyncio.Event()

            # 监听 threading.Event → asyncio.Event 的桥接
            def _on_thread_stop() -> None:
                async_stop.set()

            # 启动监听线程
            watch_thread = threading.Thread(
                target=lambda: (stop_event.wait(), _on_thread_stop()),
                daemon=True,
            )
            watch_thread.start()

            logger.info("Starting agent core and local API server")
            try:
                await asyncio.gather(
                    agent.run(stop_event=async_stop),
                    api.run(stop_event=async_stop),
                )
            except asyncio.CancelledError:
                logger.info("Service main tasks cancelled")
            finally:
                logger.info("SAUAgentService stopped")

        # ------------------------------------------------------------------
        # SvcStop：服务停止
        # ------------------------------------------------------------------
        def SvcStop(self) -> None:
            """服务停止：通知 SCM 正在停止 → 置停止标志 → 等待清理完成。"""
            # 报告 SERVICE_STOP_PENDING 并告知 SCM 预计需要 30 秒完成清理
            # 注意：参数名是 waitHint（非 wait_hint），否则控制线程抛 TypeError 被吞掉导致 stop 无效
            self.ReportServiceStatus(
                win32service.SERVICE_STOP_PENDING,
                waitHint=30000,
            )
            logger.info("SAUAgentService stop requested")
            if self._stop_event is not None:
                self._stop_event.set()

else:
    # ---------------------------------------------------------------------------
    # 非 Windows / 无 pywin32 开发环境 stub
    # ---------------------------------------------------------------------------
    class SAUAgentService:  # type: ignore[no-redef]
        """开发环境 stub（无 pywin32）。"""
        _svc_name_ = "SAUAgentService"
        _svc_display_name_ = "SAU Publish Agent"

        def __init__(self, args=None):
            pass

        def SvcDoRun(self):
            print("[stub] SAUAgentService.SvcDoRun — pywin32 not available")

        def SvcStop(self):
            print("[stub] SAUAgentService.SvcStop — pywin32 not available")


# ---------------------------------------------------------------------------
# 服务安装/卸载/控制辅助函数（供 sau_ops.py 调用）
# ---------------------------------------------------------------------------
def install_service() -> None:
    """安装 SAUAgentService。"""
    if not _HAS_PYWIN32:
        raise RuntimeError("pywin32 is required to install the service")
    _exe = str(Path(sys.argv[0]).resolve()) if sys.argv and sys.argv[0].lower().endswith(".exe") else sys.executable
    exe_path = str(Path(_exe).resolve().parent / "sau-service.exe")
    import subprocess
    subprocess.check_call([exe_path, "install"])
    print(f"Service '{SAUAgentService._svc_name_}' installed.")


def uninstall_service() -> None:
    """停止并卸载 SAUAgentService。"""
    if not _HAS_PYWIN32:
        raise RuntimeError("pywin32 is required to uninstall the service")
    _exe = str(Path(sys.argv[0]).resolve()) if sys.argv and sys.argv[0].lower().endswith(".exe") else sys.executable
    exe_path = str(Path(_exe).resolve().parent / "sau-service.exe")
    import subprocess
    subprocess.check_call([exe_path, "remove"])
    print(f"Service '{SAUAgentService._svc_name_}' uninstalled.")


def start_service() -> None:
    """启动服务（直接调用 win32serviceutil，兼容 Nuitka 编译环境）。"""
    if not _HAS_PYWIN32:
        raise RuntimeError("pywin32 is required")
    win32serviceutil.StartService(SAUAgentService._svc_name_)


def stop_service() -> None:
    """停止服务（直接调用 win32serviceutil，兼容 Nuitka 编译环境）。"""
    if not _HAS_PYWIN32:
        raise RuntimeError("pywin32 is required")
    win32serviceutil.StopService(SAUAgentService._svc_name_)


def restart_service() -> None:
    """重启服务。"""
    stop_service()
    start_service()


def get_service_status() -> str:
    """查询服务状态，返回状态字符串。"""
    if not _HAS_PYWIN32:
        return "unknown (pywin32 not available)"
    try:
        import win32service  # type: ignore[import-untyped]
        import win32serviceutil  # type: ignore[import-untyped]
        status_code = win32serviceutil.QueryServiceStatus(SAUAgentService._svc_name_)[1]
        status_map = {
            win32service.SERVICE_STOPPED: "stopped",
            win32service.SERVICE_START_PENDING: "starting",
            win32service.SERVICE_RUNNING: "running",
            win32service.SERVICE_PAUSE_PENDING: "pausing",
            win32service.SERVICE_PAUSED: "paused",
            win32service.SERVICE_CONTINUE_PENDING: "resuming",
            win32service.SERVICE_STOP_PENDING: "stopping",
        }
        return status_map.get(status_code, f"unknown({status_code})")
    except Exception as e:
        return f"not installed ({e})"


# ---------------------------------------------------------------------------
# 极早期崩溃日志（在 import conf / pywin32 之前，捕获 Nuitka 产物启动崩溃）
# ---------------------------------------------------------------------------
def _early_log(msg: str) -> None:
    """向 install 目录下的 crash.log 追加一行（极早期，不依赖任何第三方）。"""
    try:
        import datetime
        exe_dir = Path(sys.executable if sys.executable.lower().endswith(".exe") else __file__).resolve().parent
        log_file = exe_dir / "sau-service-crash.log"
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass  # 极端情况：连日志都写不了，放弃


# ---------------------------------------------------------------------------
# pywin32 服务入口点（python service_host.py install/start/stop/remove）
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        _early_log(f"=== sau-service.exe started ===")
        _early_log(f"sys.executable = {sys.executable}")
        _early_log(f"sys.argv = {sys.argv}")
        _early_log(f"__file__ = {__file__}")
        _early_log(f"_PROJECT_ROOT = {_PROJECT_ROOT}")
        _early_log(f"_HAS_PYWIN32 = {_HAS_PYWIN32}")
        _early_log(f"SAU_HOME = {SAU_HOME}")
        _early_log(f"python path = {sys.path[:5]}")

        if not _HAS_PYWIN32:
            _early_log("ERROR: pywin32 is NOT available!")
            print("pywin32 is not installed. Cannot run as Windows service.")
            sys.exit(1)

        # ── 解析命令 ──
        _cmd = sys.argv[1].lower() if len(sys.argv) > 1 else ""

        # ── install: 直接用 win32service API 注册服务（绕过 HandleCommandLine）──
        #    HandleCommandLine("install") 内部调用 LocatePythonServiceExe() 查找
        #    pythonservice.exe，但 Nuitka standalone 下不存在该文件。
        if _cmd == "install":
            # SCM 存储的 binary path 加引号：默认安装目录 "Program Files" 含空格，
            # 未加引号属 unquoted service path 安全问题。仅影响 SCM 记录，
            # install/uninstall 的 subprocess 调用不受影响。
            _exe_path = f'"{Path(sys.argv[0]).resolve()}"'
            _early_log(f"Direct install: binary={_exe_path}")

            def _apply_service_config(_svc) -> None:
                """写入启动类型/路径/显示名/描述（新建与 1073 回退更新共用）。"""
                import win32service as _ws
                _ws.ChangeServiceConfig(
                    _svc,
                    _ws.SERVICE_NO_CHANGE,  # 第 2 参是服务类型（非 desired access），保持原类型不变
                    _ws.SERVICE_AUTO_START,
                    _ws.SERVICE_ERROR_NORMAL,
                    _exe_path,
                    None, 0, None, None, None,  # loadOrderGroup / dwTagId(须为 int) / dependencies / account / password
                    SAUAgentService._svc_display_name_,
                )
                # 服务描述需通过 ChangeServiceConfig2(SERVICE_CONFIG_DESCRIPTION) 设置
                _ws.ChangeServiceConfig2(
                    _svc,
                    _ws.SERVICE_CONFIG_DESCRIPTION,
                    SAUAgentService._svc_description_,
                )

            try:
                import win32service
                import winerror
                scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
                try:
                    svc = win32service.CreateService(
                        scm,
                        SAUAgentService._svc_name_,
                        SAUAgentService._svc_display_name_,
                        win32service.SERVICE_ALL_ACCESS,
                        win32service.SERVICE_WIN32_OWN_PROCESS,
                        win32service.SERVICE_AUTO_START,
                        win32service.SERVICE_ERROR_NORMAL,
                        _exe_path,
                        None,  # no load ordering group
                        0,     # dwTagId：pywin32 要求 int（0=不分配 tag），传 None 会抛 TypeError
                        None,  # no dependencies
                        None,  # LocalSystem account
                        None,  # no password
                    )
                    _created = True
                except win32service.error as e:
                    if e.winerror != winerror.ERROR_SERVICE_EXISTS:
                        raise
                    # install 幂等回退：服务已存在（1073）时改为更新配置
                    # （对齐 pywin32 HandleCommandLine install→update 行为）
                    _early_log(f"Service already exists (1073), falling back to update: {e}")
                    svc = win32service.OpenService(
                        scm, SAUAgentService._svc_name_, win32service.SERVICE_ALL_ACCESS,
                    )
                    _created = False
                _apply_service_config(svc)
                win32service.CloseServiceHandle(svc)
                win32service.CloseServiceHandle(scm)
                _early_log(f"Service '{SAUAgentService._svc_name_}' {'installed' if _created else 'updated'} successfully")
                print(f"{'Installing' if _created else 'Updating'} service {SAUAgentService._svc_name_}")
                print(f"Service {'installed' if _created else 'updated'} successfully.")
            except Exception as e:
                _early_log(f"Direct install failed: {e!r}")
                raise

        # ── remove: 直接用 win32service API 删除服务 ──
        elif _cmd == "remove":
            _early_log("Direct remove")
            try:
                import win32service
                scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
                try:
                    svc = win32service.OpenService(
                        scm, SAUAgentService._svc_name_,
                        win32service.SERVICE_ALL_ACCESS,
                    )
                    win32service.DeleteService(svc)
                    win32service.CloseServiceHandle(svc)
                    _early_log(f"Service '{SAUAgentService._svc_name_}' removed")
                    print(f"Service '{SAUAgentService._svc_name_}' removed.")
                except win32service.error as e:
                    _early_log(f"Service not found (already removed): {e}")
                    print(f"Service '{SAUAgentService._svc_name_}' not found.")
                finally:
                    win32service.CloseServiceHandle(scm)
            except Exception as e:
                _early_log(f"Direct remove failed: {e!r}")
                raise

        # ── 无参数: SCM 拉起服务进程，直接进入服务控制 dispatcher ──
        #    注意：不能走 HandleCommandLine——它在无参数时调用 usage() 并 exit 1，
        #    不会进入 StartServiceCtrlDispatcher，导致服务启动超时（事件 7009/7000）。
        elif _cmd == "":
            _early_log("Service dispatcher mode (launched by SCM)")
            try:
                servicemanager.Initialize()
                servicemanager.PrepareToHostSingle(SAUAgentService)
                servicemanager.StartServiceCtrlDispatcher()
                _early_log("Service dispatcher exited")
            except Exception as e:
                _early_log(f"Service dispatcher failed: {e!r}")
                raise

        # ── start/stop/update 等: 使用 HandleCommandLine ──
        #    这些命令不需要 pythonservice.exe。
        #    退出码契约：HandleCommandLine 失败不抛异常而是返回 winerror 整数，
        #    必须接收返回值并在非零时 exit(1)，否则失败会被误判为成功。
        else:
            _early_log(f"Running HandleCommandLine with args: {sys.argv[1:]}")
            try:
                import winerror
                _err = win32serviceutil.HandleCommandLine(SAUAgentService)
                # 幂等特判（重装/修复安装与卸载链路需要）：
                #   start 已运行的服务 → 1056；stop 已停止的服务 → 1062，均按成功处理
                if _cmd == "start" and _err == winerror.ERROR_SERVICE_ALREADY_RUNNING:
                    _early_log("Service already running (1056), treating start as success")
                    _err = 0
                elif _cmd == "stop" and _err == winerror.ERROR_SERVICE_NOT_ACTIVE:
                    _early_log("Service not active (1062), treating stop as success")
                    _err = 0
                if _err:
                    _early_log(f"HandleCommandLine failed with winerror: {_err}")
                    sys.exit(1)
                _early_log("HandleCommandLine completed successfully")
            except SystemExit as e:
                _code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
                _early_log(f"HandleCommandLine exited with code: {_code}")
                if _code:
                    sys.exit(_code)  # 非零退出码必须传播，不得静默降为 0
            except Exception as e:
                _early_log(f"HandleCommandLine crashed: {e!r}")
                import traceback
                _early_log(traceback.format_exc())
                raise
    except Exception:
        # 兜底：即使 _early_log 本身出问题，也尝试写崩溃日志
        try:
            import datetime as _dt
            import traceback as _tb
            _exe_dir = Path(sys.executable if sys.executable.lower().endswith(".exe") else __file__).resolve().parent
            with open(_exe_dir / "sau-service-crash.log", "a", encoding="utf-8") as _f:
                _f.write(f"\n[{_dt.datetime.now():%Y-%m-%d %H:%M:%S}] !!! UNCAUGHT EXCEPTION !!!\n")
                _f.write(_tb.format_exc())
        except Exception:
            pass
        # 失败必须以非零退出码结束，否则安装脚本（post-install.bat）会误判为成功
        sys.exit(1)
