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
import time
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
    # 计算崩溃日志路径（保留已写崩溃文件路径的记录）
    _crash_path = None
    try:
        _exe = sys.executable if sys.executable.lower().endswith(".exe") else None
        if _exe:
            _crash_path = Path(_exe).resolve().parent / "sau-service-crash.log"
        else:
            _crash_path = Path(os.environ.get("TEMP", ".")) / "sau-service-crash.log"
    except Exception:
        pass
    _crash_log_write("=== UNCAUGHT EXCEPTION (module-level) ===")
    for line in traceback.format_exception(exc_type, exc_value, exc_tb):
        _crash_log_write(line.rstrip())
    # 为什么：崩溃文件路径需要同时进入 logger（如果日志系统可用），方便排障人员直接去对应文件
    try:
        logger.error("Uncaught module-level exception — crash written to: %s", _crash_path,
                     exc_info=(exc_type, exc_value, exc_tb))
    except Exception:
        pass
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

# ---------------------------------------------------------------------------
# 强制 WindowsSelectorEventLoopPolicy（Windows 平台）
# ---------------------------------------------------------------------------
# 为什么必须全局强制：
#   Python 3.8+ Windows 默认使用 WindowsProactorEventLoopPolicy，
#   它基于 I/O Completion Port 管理匿名管道 stdin/stdout/stderr 子进程。
#   用户提供的报错日志显示了典型的 Proactor 竞态：
#     _ProactorBasePipeTransport._call_connection_lost(None)
#     → base_events._detach → _wakeup
#     TypeError: 'NoneType' object is not iterable
#   这是 WindowsProactorEventLoop 在快速关闭匿名管道 transport 时的已知问题，
#   当 asyncio.run 退栈、事件循环被 close() 调用时，如果 pipe transport 的
#   _protocol 之一已被置为 None，会在 _wakeup(writer, reader) 里迭代 None 抛异常。
#   最干净的修复方案（不需要 monkey patch 私有属性）是：
#   在所有 asyncio.run / get_event_loop 之前就直接切换到 SelectorEventLoopPolicy，
#   让项目完全绕开 _ProactorBasePipeTransport 类的代码路径。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]

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
            from sau_agent_pkg.version import APP_VERSION
            # 为什么：服务启动第一条日志，确认"是哪个服务实例/版本/在哪跑"，服务排障第一入口
            logger.info("SvcDoRun start: service=%s display=%s version=%s SAU_HOME=%s",
                        self._svc_name_, self._svc_display_name_, APP_VERSION, SAU_HOME)

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
            except Exception as e:  # 为什么：DB 失败后续读写全部会挂，明确 error + 根因
                logger.error("init_db failed in SvcDoRun: %s", e, exc_info=True)

            # 生成 local_token（供托盘进程读取）
            from sau_agent_pkg.config import generate_local_token
            try:
                token = generate_local_token()
                logger.info("Local token generated: length=%d", len(token) if token else 0)
            except Exception as e:  # 为什么：token 生成失败托盘无法连本地 API，需明确 error
                logger.error("generate_local_token failed in SvcDoRun: %s", e, exc_info=True)

            # 为什么：_main 启动是 asyncio 生命周期起点，与退出日志配对可计算服务 uptime
            logger.info("About to enter _main asyncio loop (asyncio.run)")
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

            # 为什么：_main 进入日志，可计算 asyncio 初始化耗时
            logger.info("_main entered: loading config and constructing core objects")
            config = load_config()
            agent = SauAgentCore(config=config)
            api = LocalApiServer(core=agent)

            # 半自动更新接线（M5）：upgrade_notice → 下载/校验/状态
            agent.on_upgrade_notice = updater.handle_upgrade_notice
            logger.info("Upgrade notice callback wired (agent.on_upgrade_notice → updater.handle_upgrade_notice)")
            try:
                updater.cleanup_expired()
            except Exception:
                logger.exception("Upgrade cleanup failed")

            # 将 threading.Event 包装为 asyncio.Event 供内部使用
            async_stop = asyncio.Event()

            # 监听 threading.Event → asyncio.Event 的桥接
            def _on_thread_stop() -> None:
                # 为什么：SvcStop 置位 stop_event → watch_thread 唤醒 → 此处触发 async_stop，记录 stop 推进到 asyncio 侧的节点
                logger.info("watch_thread: threading.Event fired, async_stop will be set (service stop propagation)")
                async_stop.set()

            # 启动监听线程
            watch_thread = threading.Thread(
                target=lambda: (stop_event.wait(), _on_thread_stop()),
                daemon=True,
            )
            watch_thread.start()
            # 为什么：确认 watch_thread 已就绪（未启动 stop 会丢失），同时输出线程标识方便死锁排查
            logger.info("watch_thread started (daemon=%s, ident=%s) to bridge SvcStop → async_stop",
                        watch_thread.daemon, watch_thread.ident)

            logger.info("Starting agent core and local API server")
            try:
                await asyncio.gather(
                    agent.run(stop_event=async_stop),
                    api.run(stop_event=async_stop),
                )
            except asyncio.CancelledError:
                # 为什么：CancelledError 是正常 stop 路径，info 级别让用户知道是"被 cancel"而非异常退出
                logger.info("Service main tasks cancelled (gather CancelledError, expected during stop)")
            except Exception as e:
                # 为什么：非 CancelledError 的异常都意味着某子任务崩溃，error 带 trace 排障
                logger.error("_main asyncio.gather failed: %s", e, exc_info=True)
                raise
            finally:
                # 为什么：不管成功/失败，记录 stop_event 是否已被置位，区分"主动 stop"与"异常崩溃"
                logger.info("SAUAgentService stopped (stop_event.is_set=%s)",
                            stop_event.is_set() if stop_event else "N/A")

        # ------------------------------------------------------------------
        # SvcStop：服务停止
        # ------------------------------------------------------------------
        def SvcStop(self) -> None:
            """服务停止：通知 SCM 正在停止 → 置停止标志 → 启动 watchdog 自杀线程 → 等待清理完成。"""
            # 为什么：SCM 要求服务立即响应，第一条日志确认 SvcStop 确实被回调到了（某些情况下服务会卡死在 SCM 层面）
            logger.info("SvcStop called by SCM, about to report SERVICE_STOP_PENDING")
            # 报告 SERVICE_STOP_PENDING 并告知 SCM 预计需要 30 秒完成清理
            # 注意：参数名是 waitHint（非 wait_hint），否则控制线程抛 TypeError 被吞掉导致 stop 无效
            self.ReportServiceStatus(
                win32service.SERVICE_STOP_PENDING,
                waitHint=30000,
            )
            logger.info("SAUAgentService stop requested")
            if self._stop_event is not None:
                # 为什么：stop_event 被置位是服务真正"开始关闭"的信号，明确记录避免怀疑 stop 没生效
                logger.info("stop_event.set() called, watch_thread should wake up soon")
                self._stop_event.set()
            else:
                logger.warning("SvcStop: self._stop_event is None (SvcDoRun may not have completed init)")

            # ------------------------------------------------------------------
            # 服务端 watchdog 自杀线程（客户端 taskkill 的服务端双保险）
            # ------------------------------------------------------------------
            # 为什么必须加这个看门狗：
            #   即使服务端设置了 WindowsSelectorEventLoopPolicy，
            #   仍可能因用户写的 coroutine 卡死 / 第三方库 deadlock
            #   导致 asyncio.run(self._main(...)) 根本退不出来 →
            #   结果就是 SCM 一直显示 SERVICE_STOP_PENDING（stopping）
            #   就是用户日志里出现的 35 条 status_code=3 stopping，持续 30s+。
            #   这里起一条 daemon 线程（不阻塞 ServiceFrame 的 SvcStop 返回），
            #   25s 后不管任何状态直接 os._exit(0) 强制杀自己，
            #   进程退出 → SCM 立刻把服务切到 stopped（SCM 监控宿主进程句柄）。
            #   选 25s：给 SCM waitHint=30000 留 5s 余量，又在客户端 30s 等待超时前。
            def _watchdog_suicide() -> None:
                try:
                    time.sleep(25)
                except Exception:
                    pass
                try:
                    logger.error(
                        "SvcStop watchdog: stop_event 下发已 25s 但宿主仍未退出，"
                        "强制 os._exit(0) 自杀，避免 SCM 一直 stopping。"
                    )
                except Exception:
                    pass
                # 注意：用 os._exit 而不是 sys.exit —— sys.exit 只会抛 SystemExit
                # 异常，若当前 asyncio.run 外被大 except 吞掉就不会真正退出。
                # os._exit 直接调用 Windows ExitProcess，不会走清理钩子，
                # 在 watchdog 这种"最后兜底"场景就是我们要的。
                try:
                    os._exit(0)
                except Exception:
                    pass

            threading.Thread(target=_watchdog_suicide, daemon=True,
                             name="SAU-SvcStop-Watchdog").start()

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
    import subprocess
    if not _HAS_PYWIN32:
        raise RuntimeError("pywin32 is required to install the service")
    _exe = str(Path(sys.argv[0]).resolve()) if sys.argv and sys.argv[0].lower().endswith(".exe") else sys.executable
    exe_path = str(Path(_exe).resolve().parent / "sau-service.exe")
    cmd = [exe_path, "install"]
    # 为什么：记录调用方、命令行，便于复现安装命令并查权限问题
    logger.info("install_service: executing cmd=%s (exe_path=%s)", cmd, exe_path)
    try:
        subprocess.check_call(cmd)
        # 为什么：子进程无异常即视为安装成功，打印并打 info 配对
        logger.info("install_service: succeeded via subprocess (exit 0)")
        print(f"Service '{SAUAgentService._svc_name_}' installed.")
    except subprocess.CalledProcessError as e:
        # 为什么：安装失败最常见是权限/UAC，记录退出码让用户快速对应 Windows Installer 错误
        logger.error("install_service failed: cmd=%s, exitcode=%s", cmd, e.returncode, exc_info=True)
        raise
    except Exception as e:
        logger.error("install_service failed with unexpected error: %s", e, exc_info=True)
        raise


def uninstall_service() -> None:
    """停止并卸载 SAUAgentService。"""
    import subprocess
    if not _HAS_PYWIN32:
        raise RuntimeError("pywin32 is required to uninstall the service")
    _exe = str(Path(sys.argv[0]).resolve()) if sys.argv and sys.argv[0].lower().endswith(".exe") else sys.executable
    exe_path = str(Path(_exe).resolve().parent / "sau-service.exe")
    cmd = [exe_path, "remove"]
    # 为什么：卸载命令需要 SCM 权限，记录命令行便于排障与审计
    logger.info("uninstall_service: executing cmd=%s (exe_path=%s)", cmd, exe_path)
    try:
        subprocess.check_call(cmd)
        logger.info("uninstall_service: succeeded via subprocess (exit 0)")
        print(f"Service '{SAUAgentService._svc_name_}' uninstalled.")
    except subprocess.CalledProcessError as e:
        logger.error("uninstall_service failed: cmd=%s, exitcode=%s", cmd, e.returncode, exc_info=True)
        raise
    except Exception as e:
        logger.error("uninstall_service failed with unexpected error: %s", e, exc_info=True)
        raise


def start_service() -> None:
    """启动服务（直接调用 win32serviceutil，兼容 Nuitka 编译环境）。"""
    if not _HAS_PYWIN32:
        raise RuntimeError("pywin32 is required")
    # 为什么：启动前打日志，确认"谁发起的 start"（sau-ops 还是托盘），避免多源启动互相干扰
    logger.info("start_service: calling win32serviceutil.StartService(%s)", SAUAgentService._svc_name_)
    try:
        win32serviceutil.StartService(SAUAgentService._svc_name_)
        logger.info("start_service: SCM accepted start request (service may still be START_PENDING)")
    except Exception as e:
        # 为什么：start 失败典型原因 1056/1058/权限不足，记录完整异常
        logger.error("start_service failed: %s", e, exc_info=True)
        raise


def stop_service() -> None:
    """停止服务（直接调用 win32serviceutil，兼容 Nuitka 编译环境）。"""
    if not _HAS_PYWIN32:
        raise RuntimeError("pywin32 is required")
    # 为什么：stop 通常在升级/卸载前触发，记录时间点可用来校验"升级前服务确实已停"
    logger.info("stop_service: calling win32serviceutil.StopService(%s)", SAUAgentService._svc_name_)
    try:
        win32serviceutil.StopService(SAUAgentService._svc_name_)
        logger.info("stop_service: SCM accepted stop request (service may still be STOP_PENDING)")
    except Exception as e:
        logger.error("stop_service failed: %s", e, exc_info=True)
        raise


def restart_service() -> None:
    """重启服务。"""
    # 为什么：restart 是 stop+start 组合，拆成两步记录，失败时知道卡在哪步
    logger.info("restart_service: step 1/2 stop_service")
    stop_service()
    logger.info("restart_service: step 2/2 start_service")
    start_service()
    logger.info("restart_service: both steps completed (requests accepted by SCM)")


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
        status_str = status_map.get(status_code, f"unknown({status_code})")
        # 为什么：频繁查询服务状态（tray 轮询）时可确认状态是否抖动，info 级别方便留痕
        logger.info("get_service_status: service=%s status_code=%d → %s",
                    SAUAgentService._svc_name_, status_code, status_str)
        return status_str
    except Exception as e:
        # 为什么：查询失败通常意味着"服务未安装"或"权限不足"，warning 不阻塞调用方但提示排障
        logger.warning("get_service_status query failed: %s", e)
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
        # 为什么：保留崩溃文件路径的记录（全局变量缓存），以便后续 logger 可用时上报
        global _EARLY_LOG_PATH  # noqa: PLW0603
        _EARLY_LOG_PATH = str(log_file)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass  # 极端情况：连日志都写不了，放弃


_EARLY_LOG_PATH: str | None = None


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

        # 为什么：一旦 SAU_HOME 可用就立刻初始化标准 logger，后续流程用 logger 代替 _early_log
        try:
            _setup_logging()
            logger.info("__main__: logger initialized (early_log_path=%s, _LOG_FILE=%s)",
                        _EARLY_LOG_PATH, _LOG_FILE)
        except Exception as _e:
            _early_log(f"__main__: _setup_logging failed: {_e!r}")

        if not _HAS_PYWIN32:
            _early_log("ERROR: pywin32 is NOT available!")
            try:
                logger.error("__main__: pywin32 is NOT available, cannot run as Windows service")
            except Exception:
                pass
            print("pywin32 is not installed. Cannot run as Windows service.")
            sys.exit(1)

        # ── 解析命令 ──
        _cmd = sys.argv[1].lower() if len(sys.argv) > 1 else ""
        logger.info("__main__: parsed command=%s, argv=%s", _cmd or "(empty/SCM dispatcher)", sys.argv[1:])

        # ── install: 直接用 win32service API 注册服务（绕过 HandleCommandLine）──
        #    HandleCommandLine("install") 内部调用 LocatePythonServiceExe() 查找
        #    pythonservice.exe，但 Nuitka standalone 下不存在该文件。
        if _cmd == "install":
            # SCM 存储的 binary path 加引号：默认安装目录 "Program Files" 含空格，
            # 未加引号属 unquoted service path 安全问题。仅影响 SCM 记录，
            # install/uninstall 的 subprocess 调用不受影响。
            _exe_path = f'"{Path(sys.argv[0]).resolve()}"'
            _early_log(f"Direct install: binary={_exe_path}")
            # 为什么：install 是最容易因权限失败的 SCM 操作，第一行 info 记录 binary path 便于复现
            logger.info("SCM install branch: binary_path=%s, service=%s", _exe_path, SAUAgentService._svc_name_)

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
                logger.info("SCM install: OpenSCManager + CreateService attempting...")
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
                    # 为什么：幂等特判 1073 是修复安装/重复 install 的关键路径，必须 info 留痕
                    logger.info("SCM install: idempotent fallback — ERROR_SERVICE_EXISTS (1073) → open+update instead of create")
                    svc = win32service.OpenService(
                        scm, SAUAgentService._svc_name_, win32service.SERVICE_ALL_ACCESS,
                    )
                    _created = False
                _apply_service_config(svc)
                win32service.CloseServiceHandle(svc)
                win32service.CloseServiceHandle(scm)
                _result = 'installed' if _created else 'updated'
                _early_log(f"Service '{SAUAgentService._svc_name_}' {_result} successfully")
                # 为什么：SCM 操作成功，记录最终结果（install vs update）供审计
                logger.info("SCM install: success — service=%s action=%s", SAUAgentService._svc_name_, _result)
                print(f"{'Installing' if _created else 'Updating'} service {SAUAgentService._svc_name_}")
                print(f"Service {'installed' if _created else 'updated'} successfully.")
            except Exception as e:
                _early_log(f"Direct install failed: {e!r}")
                # 为什么：install 失败通常是权限/目录问题，error 带 trace 便于定位
                logger.error("SCM install failed: %s", e, exc_info=True)
                raise

        # ── remove: 直接用 win32service API 删除服务 ──
        elif _cmd == "remove":
            _early_log("Direct remove")
            # 为什么：remove 是破坏性操作，必须 info 留痕审计
            logger.info("SCM remove branch: service=%s", SAUAgentService._svc_name_)
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
                    logger.info("SCM remove: success — service=%s deleted from SCM", SAUAgentService._svc_name_)
                    print(f"Service '{SAUAgentService._svc_name_}' removed.")
                except win32service.error as e:
                    _early_log(f"Service not found (already removed): {e}")
                    # 为什么：remove 幂等——服务不存在不算失败，warning 提示即可
                    logger.warning("SCM remove: service not found (already removed?), winerror=%s — treated as success",
                                   getattr(e, "winerror", "N/A"))
                    print(f"Service '{SAUAgentService._svc_name_}' not found.")
                finally:
                    win32service.CloseServiceHandle(scm)
            except Exception as e:
                _early_log(f"Direct remove failed: {e!r}")
                logger.error("SCM remove failed: %s", e, exc_info=True)
                raise

        # ── 无参数: SCM 拉起服务进程，直接进入服务控制 dispatcher ──
        #    注意：不能走 HandleCommandLine——它在无参数时调用 usage() 并 exit 1，
        #    不会进入 StartServiceCtrlDispatcher，导致服务启动超时（事件 7009/7000）。
        elif _cmd == "":
            _early_log("Service dispatcher mode (launched by SCM)")
            # 为什么：SCM 拉起路径最容易出"启动超时"问题，进入 dispatcher 前后打 info 可算耗时
            logger.info("SCM dispatcher branch (empty cmd): entering StartServiceCtrlDispatcher...")
            try:
                servicemanager.Initialize()
                servicemanager.PrepareToHostSingle(SAUAgentService)
                servicemanager.StartServiceCtrlDispatcher()
                _early_log("Service dispatcher exited")
                logger.info("SCM dispatcher: StartServiceCtrlDispatcher returned (service process about to exit)")
            except Exception as e:
                _early_log(f"Service dispatcher failed: {e!r}")
                # 为什么：dispatcher 失败服务就起不来，error 带 trace 是排障关键
                logger.error("SCM dispatcher failed: %s", e, exc_info=True)
                raise

        # ── start/stop/update 等: 使用 HandleCommandLine ──
        #    这些命令不需要 pythonservice.exe。
        #    退出码契约：HandleCommandLine 失败不抛异常而是返回 winerror 整数，
        #    必须接收返回值并在非零时 exit(1)，否则失败会被误判为成功。
        else:
            _early_log(f"Running HandleCommandLine with args: {sys.argv[1:]}")
            # 为什么：HandleCommandLine 负责 start/stop 等，记录命令行便于复现
            logger.info("HandleCommandLine branch: cmd=%s, args=%s", _cmd, sys.argv[1:])
            try:
                import winerror
                _err = win32serviceutil.HandleCommandLine(SAUAgentService)
                # 为什么：HandleCommandLine 返回 winerror（不抛异常），必须 info 留痕返回值
                logger.info("HandleCommandLine returned raw winerror=%s (cmd=%s)", _err, _cmd)
                # 幂等特判（重装/修复安装与卸载链路需要）：
                #   start 已运行的服务 → 1056；stop 已停止的服务 → 1062，均按成功处理
                if _cmd == "start" and _err == winerror.ERROR_SERVICE_ALREADY_RUNNING:
                    _early_log("Service already running (1056), treating start as success")
                    # 为什么：1056 幂等特判（服务已运行），避免安装脚本误判失败
                    logger.info("HandleCommandLine idempotent rule 1056: start already running service → success (err reset 0)")
                    _err = 0
                elif _cmd == "stop" and _err == winerror.ERROR_SERVICE_NOT_ACTIVE:
                    _early_log("Service not active (1062), treating stop as success")
                    # 为什么：1062 幂等特判（服务已停止），避免卸载脚本误判失败
                    logger.info("HandleCommandLine idempotent rule 1062: stop already stopped service → success (err reset 0)")
                    _err = 0
                if _err:
                    _early_log(f"HandleCommandLine failed with winerror: {_err}")
                    # 为什么：非幂等范围内的失败，error 带码值
                    logger.error("HandleCommandLine failed: winerror=%s cmd=%s → will exit(1)", _err, _cmd)
                    sys.exit(1)
                _early_log("HandleCommandLine completed successfully")
                logger.info("HandleCommandLine completed successfully (cmd=%s)", _cmd)
            except SystemExit as e:
                _code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
                _early_log(f"HandleCommandLine exited with code: {_code}")
                # 为什么：HandleCommandLine 内部可能 sys.exit，记录退出码区分"正常 0"和"失败非 0"
                logger.info("HandleCommandLine raised SystemExit: code=%s", _code)
                if _code:
                    sys.exit(_code)  # 非零退出码必须传播，不得静默降为 0
            except Exception as e:
                _early_log(f"HandleCommandLine crashed: {e!r}")
                import traceback
                _early_log(traceback.format_exc())
                # 为什么：完全意料外的崩溃，error 带 trace 便于 pywin32 版本/兼容性排障
                logger.error("HandleCommandLine crashed with unexpected exception: %s", e, exc_info=True)
                raise
    except Exception:
        # 兜底：即使 _early_log 本身出问题，也尝试写崩溃日志
        try:
            import datetime as _dt
            import traceback as _tb
            _exe_dir = Path(sys.executable if sys.executable.lower().endswith(".exe") else __file__).resolve().parent
            _crash_file = _exe_dir / "sau-service-crash.log"
            with open(_crash_file, "a", encoding="utf-8") as _f:
                _f.write(f"\n[{_dt.datetime.now():%Y-%m-%d %H:%M:%S}] !!! UNCAUGHT EXCEPTION !!!\n")
                _f.write(_tb.format_exc())
            # 为什么：最后兜底也要把崩溃文件路径报给 logger（如可用）
            try:
                logger.error("__main__ uncaught exception — crash dump written to: %s", _crash_file, exc_info=True)
            except Exception:
                pass
        except Exception:
            pass
        # 失败必须以非零退出码结束，否则安装脚本（post-install.bat）会误判为成功
        sys.exit(1)
