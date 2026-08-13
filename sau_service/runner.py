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


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------
async def run_foreground() -> None:
    """前台运行 agent + local_api（调试模式）。"""
    from sau_agent_pkg.config import generate_local_token, load_config
    from sau_agent_pkg.core import SauAgentCore
    from sau_agent_pkg.db_init import init_db
    from sau_agent_pkg.local_api import LocalApiServer
    from sau_agent_pkg import updater

    # 初始化数据库
    db_path = init_db()
    logger.info("Database: %s", db_path)

    # 生成 local_token
    local_token = generate_local_token()
    logger.info("Local token: %s", local_token[:8] + "...")

    # 加载配置
    config = load_config()
    logger.info("Config: server_url=%s, agent_id=%s",
                config.get("server_url", ""), config.get("agent_id", ""))

    # 创建 agent 和 api
    agent = SauAgentCore(config=config)
    api = LocalApiServer(core=agent)

    # 半自动更新接线（M5）：与 service_host._main 保持一致
    agent.on_upgrade_notice = updater.handle_upgrade_notice
    try:
        updater.cleanup_expired()
    except Exception:
        logger.exception("Upgrade cleanup failed")

    # 停止事件
    stop_event = asyncio.Event()

    # 信号处理
    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
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
        logger.info("WS connection: %s", "connected" if connected else "disconnected")

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
