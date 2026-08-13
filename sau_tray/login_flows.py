"""
sau_tray.login_flows
~~~~~~~~~~~~~~~~~~~~
各平台有头登录流程。

在 import sau_cli 的 login_* 函数之前先调用 apply_home_shim()，
确保 cookie 路径指向 SAU_HOME/cookies/。

B 站特殊处理：弹出终端二维码。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径修正
# ---------------------------------------------------------------------------
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 垫片（必须在 import sau_cli 之前）
from sau_tray.home_shim import apply_home_shim

apply_home_shim()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 平台显示名
# ---------------------------------------------------------------------------
_PLATFORM_NAMES: dict[str, str] = {
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "tencent": "视频号",
    "kuaishou": "快手",
    "bilibili": "B站",
    "baijiahao": "百家号",
    "youtube": "YouTube",
}


# ---------------------------------------------------------------------------
# 登录执行
# ---------------------------------------------------------------------------
def do_login(platform_key: str, account_name: str = "default") -> None:
    """
    执行指定平台的有头登录流程。

    Args:
        platform_key: 平台 key（如 "douyin"）
        account_name: 账号名（默认 "default"）
    """
    platform_name = _PLATFORM_NAMES.get(platform_key, platform_key)
    logger.info("Starting login for %s (account: %s)", platform_name, account_name)

    # B 站特殊处理
    if platform_key == "bilibili":
        _login_bilibili_with_qr(account_name)
        return

    # 通用登录流程
    _login_generic(platform_key, account_name)


def _login_generic(platform_key: str, account_name: str) -> None:
    """通用平台登录：调用 upstream_adapter.login_fns[platform]。"""
    # 延迟 import upstream_adapter（它 import sau_cli，需要垫片已就位）
    from sau_agent_pkg.upstream_adapter import login_fns

    login_fn = login_fns.get(platform_key)
    if login_fn is None:
        raise RuntimeError(
            f"平台 '{platform_key}' 暂无登录功能。"
            f"支持的平台: {', '.join(login_fns.keys())}"
        )

    platform_name = _PLATFORM_NAMES.get(platform_key, platform_key)
    logger.info("Launching headed browser for %s login...", platform_name)

    # 登录函数是 async 的，需要 asyncio.run
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # 大多数 login_* 函数签名：login_xxx_account(account_name, headless=False, ...)
            result = loop.run_until_complete(
                login_fn(account_name, headless=False)
            )
            logger.info("Login completed for %s: %s", platform_name, result)
        finally:
            loop.close()
    except Exception as e:
        logger.exception("Login failed for %s", platform_name)
        raise RuntimeError(f"{platform_name} 登录失败: {e}") from e


def _login_bilibili_with_qr(account_name: str) -> None:
    """
    B 站特殊登录：弹出浏览器显示二维码。
    """
    from sau_agent_pkg.upstream_adapter import login_fns

    login_fn = login_fns.get("bilibili")
    if login_fn is None:
        raise RuntimeError("B 站登录功能不可用")

    logger.info("Launching Bilibili QR code login...")

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # B 站登录：headless=False 会弹出浏览器显示二维码
            print("\n" + "=" * 50)
            print("  B 站登录 — 请在浏览器中扫描二维码")
            print("=" * 50 + "\n")

            result = loop.run_until_complete(
                login_fn(account_name, headless=False)
            )
            logger.info("Bilibili login completed: %s", result)
            print("\n  B 站登录成功！\n")
        finally:
            loop.close()
    except Exception as e:
        logger.exception("Bilibili login failed")
        raise RuntimeError(f"B 站登录失败: {e}") from e


# ---------------------------------------------------------------------------
# 便捷函数：供 sau_ops.py 或外部调用
# ---------------------------------------------------------------------------
def login_platform(platform_key: str, account_name: str = "default") -> bool:
    """
    登录指定平台（带异常捕获）。

    Returns:
        登录是否成功
    """
    try:
        do_login(platform_key, account_name)
        return True
    except Exception as e:
        logger.error("Login failed for %s: %s", platform_key, e)
        return False
