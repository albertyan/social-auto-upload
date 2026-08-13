"""
sau_tray.core.config
~~~~~~~~~~~~~~~~~~~~
基础设施层 —— 常量、路径修正、崩溃日志、配置辅助。

从 tray_app.py 提取，保持严格单向依赖：不依赖 tray_app.py。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 全局崩溃捕获（最早注册，捕获模块级 import 阶段的崩溃）
# ---------------------------------------------------------------------------
def crash_log_write(msg: str) -> None:
    """向 exe 所在目录写崩溃日志（仅依赖 sys，不依赖任何项目变量）。"""
    try:
        import datetime
        exe = sys.executable if sys.executable.lower().endswith(".exe") else None
        if exe:
            log_path = Path(exe).resolve().parent / "sau-tray-crash.log"
        else:
            log_path = Path(os.environ.get("TEMP", ".")) / "sau-tray-crash.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def global_except_hook(exc_type, exc_value, exc_tb):
    """全局未捕获异常钩子 → 写崩溃日志。"""
    import traceback as _tb_mod
    crash_log_write("=== UNCAUGHT EXCEPTION (module-level) ===")
    for line in _tb_mod.format_exception(exc_type, exc_value, exc_tb):
        crash_log_write(line.rstrip())
    import traceback
    traceback.print_exception(exc_type, exc_value, exc_tb)


# ---------------------------------------------------------------------------
# 路径修正
# ---------------------------------------------------------------------------
# 扁平化安装后，tray_app.py 直接位于 SAU 安装目录（D:\Program Files\SAU\）
# Nuitka standalone: __file__ 可能未定义，依次回退 sys.argv[0] / sys.executable
def _resolve_project_root() -> str:
    """解析项目根目录（SAU 安装目录）。"""
    try:
        return str(Path(__file__).resolve().parent)
    except NameError:
        pass
    if sys.argv and sys.argv[0].lower().endswith(".exe"):
        return str(Path(sys.argv[0]).resolve().parent)
    if sys.executable:
        return str(Path(sys.executable).resolve().parent)
    return "."


_PROJECT_ROOT = _resolve_project_root()
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 垫片（必须在 import sau_cli 之前）
from sau_tray.home_shim import SAU_HOME, apply_home_shim  # noqa: E402

apply_home_shim()

from sau_agent_pkg.version import APP_VERSION as _APP_VERSION  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
LOCAL_API_URL = "http://127.0.0.1:5410"
POLL_INTERVAL = 3  # 秒
APP_NAME = "SAU Agent"

# 平台显示名映射
PLATFORM_DISPLAY_NAMES: dict[str, str] = {
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "tencent": "视频号",
    "kuaishou": "快手",
    "bilibili": "B站",
    "baijiahao": "百家号",
    "youtube": "YouTube",
}
