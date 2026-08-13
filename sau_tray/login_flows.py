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
    logger.info("do_login 入口: platform=%s (%s), account=%s",  # 为什么打这条日志：记录登录流程入口，确认平台和账号
                platform_key, platform_name, account_name)

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
    logger.info("_login_generic: 启动 Playwright 有头浏览器用于 %s 登录（浏览器启动/关闭由 upstream login_fn 内部处理）",  # 为什么打这条日志：确认 Playwright 启动节点（内部细节由 upstream 封装）
                platform_name)

    # 登录函数是 async 的，需要 asyncio.run
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # 大多数 login_* 函数签名：login_xxx_account(account_name, headless=False, ...)
            logger.info("_login_generic: 执行 upstream %s login_fn (headless=False，会弹出浏览器引导用户登录/扫码)", platform_name)  # 为什么打这条日志：标记扫码/交互登录阶段起点
            result = loop.run_until_complete(
                login_fn(account_name, headless=False)
            )
            # 记录 cookie 写入（登录成功后 cookie 会保存到 SAU_HOME/cookies/）
            try:
                from sau_tray.home_shim import SAU_HOME
                cookie_dir = SAU_HOME / "cookies"
                cookie_files = list(cookie_dir.glob(f"*{platform_key}*")) if cookie_dir.exists() else []
                if cookie_files:
                    latest = max(cookie_files, key=lambda p: p.stat().st_mtime)
                    logger.info("cookie 写入: 平台=%s, cookie 文件路径=%s",  # 为什么打这条日志：记录登录成功后 cookie 落盘位置（排障登录态丢失）
                                platform_name, latest)
                else:
                    logger.info("cookie 写入: 平台=%s（未扫描到 cookie 文件，可能由上游默认路径保存）", platform_name)  # 为什么打这条日志：未找到 cookie 文件时的记录，方便排查路径问题
            except Exception as ce:
                logger.info("cookie 写入记录异常（不影响登录结果）: %s", ce)

            logger.info("登录成功: platform=%s (%s), result=%s", platform_key, platform_name, result)  # 为什么打这条日志：info 级确认登录成功
        finally:
            loop.close()
            logger.info("_login_generic: Playwright 会话关闭，asyncio loop 已关闭")  # 为什么打这条日志：确认 Playwright 关闭和资源清理
    except Exception as e:
        logger.error("登录失败: platform=%s (%s), error_type=%s: %s",  # 为什么打这条日志：error 级记录登录失败（平台+异常类型+信息）
                     platform_key, platform_name, type(e).__name__, e)
        raise RuntimeError(f"{platform_name} 登录失败: {e}") from e


def _login_bilibili_with_qr(account_name: str) -> None:
    """
    B 站特殊登录：弹出浏览器显示二维码。
    """
    from sau_agent_pkg.upstream_adapter import login_fns

    login_fn = login_fns.get("bilibili")
    if login_fn is None:
        raise RuntimeError("B 站登录功能不可用")

    logger.info("_login_bilibili_with_qr: 启动 Playwright 浏览器（二维码扫码阶段）")  # 为什么打这条日志：标记 B 站特殊扫码登录启动

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # B 站登录：headless=False 会弹出浏览器显示二维码
            print("\n" + "=" * 50)
            print("  B 站登录 — 请在浏览器中扫描二维码")
            print("=" * 50 + "\n")
            logger.info("扫码阶段: B 站二维码登录等待用户扫码")  # 为什么打这条日志：标记扫码交互阶段
            result = loop.run_until_complete(
                login_fn(account_name, headless=False)
            )
            # 记录 cookie 写入
            try:
                from sau_tray.home_shim import SAU_HOME
                cookie_dir = SAU_HOME / "cookies"
                cookie_files = list(cookie_dir.glob("*bilibili*")) if cookie_dir.exists() else []
                if cookie_files:
                    latest = max(cookie_files, key=lambda p: p.stat().st_mtime)
                    logger.info("cookie 写入: 平台=B 站, cookie 文件路径=%s", latest)  # 为什么打这条日志：记录 B 站 cookie 落盘
            except Exception as ce:
                logger.info("cookie 写入记录异常（不影响登录结果）: %s", ce)

            logger.info("Bilibili 登录成功: result=%s", result)  # 为什么打这条日志：确认 B 站登录成功
            print("\n  B 站登录成功！\n")
        finally:
            loop.close()
            logger.info("_login_bilibili_with_qr: Playwright 会话关闭，asyncio loop 已关闭")  # 为什么打这条日志：确认 Playwright 关闭
    except Exception as e:
        logger.error("Bilibili 登录失败: error_type=%s: %s", type(e).__name__, e)  # 为什么打这条日志：error 级记录 B 站登录失败
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
