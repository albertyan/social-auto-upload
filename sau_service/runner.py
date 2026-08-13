"""
sau_service.runner
~~~~~~~~~~~~~~~~~~
前台调试模式入口：不依赖 pywin32 服务框架，直接运行 agent + local_api。

用法：
    python -m sau_service.runner
    # 或
    python sau_service/runner.py

与 service_host.py 的 SvcDoRun 逻辑一致，但运行在用户会话（前台进程），
适合开发调试或在非 Windows 环境下测试。
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径修正
# ---------------------------------------------------------------------------
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 垫片
from sau_tray.home_shim import SAU_HOME, apply_home_shim

apply_home_shim()

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
_LOG_FILE = SAU_HOME / "logs" / "sau-runner.log"
_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def _setup_logging() -> None:
    try:  # 日志初始化失败不应导致进程崩溃，warning 后继续
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)

        # 控制台
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(ch)

        # 文件（轮转）
        fh = RotatingFileHandler(
            str(_LOG_FILE),
            maxBytes=10 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.addHandler(fh)
    except Exception as e:  # 为什么：磁盘不可写/权限不足时仍可运行，仅 warning 提示排障
        logger.warning("Logging setup partially failed: %s", e)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------
async def run_foreground() -> None:
    """前台运行 agent + local_api（调试模式）。"""
    from sau_agent_pkg.version import APP_VERSION
    from sau_agent_pkg.config import generate_local_token, load_config
    from sau_agent_pkg.core import SauAgentCore
    from sau_agent_pkg.db_init import init_db
    from sau_agent_pkg.local_api import LocalApiServer
    from sau_agent_pkg import updater

    # 为什么：启动第一行日志，确认版本与运行目录，排障时快速定位"是哪个版本在哪跑"
    logger.info("run_foreground start: version=%s, SAU_HOME=%s", APP_VERSION, SAU_HOME)

    # 初始化数据库
    try:  # 为什么：DB 初始化失败通常不可逆（磁盘/权限），需 error 明确根因
        db_path = init_db()
        logger.info("Database: %s", db_path)
    except Exception as e:
        logger.error("init_db failed: %s", e, exc_info=True)
        raise

    # 生成 local_token
    local_token = generate_local_token()
    # 为什么：token 是敏感字段，只记录前缀+长度，同时确认生成流程走到了
    logger.info("Local token generated: prefix=%s..., length=%d",
                local_token[:8] if local_token else "(empty)",
                len(local_token) if local_token else 0)

    # 加载配置
    config = load_config()
    logger.info("Config: server_url=%s, agent_id=%s",
                config.get("server_url", ""), config.get("agent_id", ""))

    # 创建 agent 和 api
    agent = SauAgentCore(config=config)
    api = LocalApiServer(core=agent)

    # 半自动更新接线（M5）：与 service_host._main 保持一致
    # 为什么：确认 upgrade_notice → handle_upgrade_notice 的接线成功，否则升级流程静默失败
    agent.on_upgrade_notice = updater.handle_upgrade_notice
    logger.info("Upgrade notice callback wired (agent.on_upgrade_notice → updater.handle_upgrade_notice)")
    try:
        updater.cleanup_expired()
    except Exception:
        logger.exception("Upgrade cleanup failed")

    # 停止事件
    stop_event = asyncio.Event()

    # 信号处理
    def _signal_handler() -> None:
        # 为什么：stop_event 被置位的唯一触发点，确认 shutdown 流是否真的收到信号
        logger.info("Shutdown signal received, stop_event will be set (wait trigger)")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows 不完全支持 add_signal_handler
            pass

    # 连接状态变更回调
    def _on_connection(connected: bool) -> None:
        # 为什么：WS 连接抖动是最常见问题，每次变化打 info 方便看"断了多久、何时重连成功"
        logger.info("WS connection state changed: %s", "CONNECTED" if connected else "DISCONNECTED")

    agent.on_connection_change = _on_connection

    logger.info("Starting SAU Agent in foreground mode (Ctrl+C to stop)")
    logger.info("Local API: http://127.0.0.1:5410")

    try:
        await asyncio.gather(
            agent.run(stop_event=stop_event),
            api.run(stop_event=stop_event),
        )
    except asyncio.CancelledError:
        pass
    except Exception as e:  # 为什么：gather 阶段任一子任务抛异常都会中断全流程，必须带异常类型+trace
        logger.error("asyncio.gather failed in run_foreground: %s", e, exc_info=True)
        raise
    finally:
        logger.info("SAU Agent stopped")


def main() -> None:
    """入口函数。"""
    _setup_logging()
    try:
        asyncio.run(run_foreground())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")


if __name__ == "__main__":
    main()
