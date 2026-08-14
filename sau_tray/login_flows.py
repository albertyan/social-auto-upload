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

    # 保存当前 event loop policy，登录完成后恢复
    # 为什么要临时切换 policy：sau_service.service_host 为了解决匿名管道竞态问题，
    # 全局设置了 WindowsSelectorEventLoopPolicy，但 SelectorEventLoop 在 Windows 上
    # 不支持子进程（create_subprocess_exec），而 Playwright 启动浏览器必须创建子进程，
    # 会抛出 NotImplementedError。所以在登录这段必须临时切回 Proactor 策略。
    old_policy = asyncio.get_event_loop_policy()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())  # type: ignore[attr-defined]
        logger.debug("_login_generic: 已临时切换为 WindowsProactorEventLoopPolicy（用于 Playwright 子进程启动）")

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
    finally:
        # 恢复原有 event loop policy，保证登录线程退出后不影响其他模块的行为约定
        asyncio.set_event_loop_policy(old_policy)
        logger.debug("_login_generic: 已恢复原有 event loop policy")


def _login_bilibili_with_qr(account_name: str) -> None:
    """
    B 站特殊登录：弹出浏览器显示二维码。
    """
    from sau_agent_pkg.upstream_adapter import login_fns

    login_fn = login_fns.get("bilibili")
    if login_fn is None:
        raise RuntimeError("B 站登录功能不可用")

    logger.info("_login_bilibili_with_qr: 启动 Playwright 浏览器（二维码扫码阶段）")  # 为什么打这条日志：标记 B 站特殊扫码登录启动

    # 保存当前 event loop policy，登录完成后恢复（原因同 _login_generic）
    old_policy = asyncio.get_event_loop_policy()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())  # type: ignore[attr-defined]
        logger.debug("_login_bilibili_with_qr: 已临时切换为 WindowsProactorEventLoopPolicy（用于 Playwright 子进程启动）")

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
    finally:
        # 恢复原有 event loop policy
        asyncio.set_event_loop_policy(old_policy)
        logger.debug("_login_bilibili_with_qr: 已恢复原有 event loop policy")


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
        # 登录成功后立即重扫 accounts（emit_events=True），让事件总线感知新增账号
        # 为什么放在 login_platform：do_login 可能被 CLI 直接调用，但也会被托盘 GUI 走；
        # 这里是通用收尾点，不改变 do_login 的签名（保持向后兼容）。
        try:
            from sau_agent_pkg import accounts as _accounts
            _accounts.scan(emit_events=True)
            logger.info("login_platform: %s/%s 登录完成，已触发账号扫描（emit_events=True），若新增账号会自动上报 account_sync",
                        platform_key, account_name)
        except Exception as ae:
            logger.debug("login_platform: 登录后触发账号扫描失败（不影响登录本身）: %s", ae)
        return True
    except Exception as e:
        logger.error("Login failed for %s: %s", platform_key, e)
        return False


def prompt_account_and_login(platform_key: str) -> bool:
    """
    托盘 GUI 登录入口：弹 tkinter simpledialog 让用户输入「账号标识名」，默认 "default"。

    为什么单独抽这个函数：
    - 托盘（pystray）的 MenuItem lambda 在 pystray 线程执行，不是桌面主线程；
      tkinter 在 pystray 线程直接弹 simpledialog 在 Windows 上没问题（子消息循环自洽），
      但必须明确放在独立线程（调用方用 Thread 包装）里，避免 pystray 菜单阻塞菜单刷新。
    - 用户点击"平台登录 -> 抖音"时，通过本函数先问账号名（支持同平台多账号并存），
      不会再像以前那样写死 default 覆盖同一个 cookie 文件。

    账号名约束（会自动过滤）：
    - 去首尾空白；空串或取消 → 使用 default
    - 去掉 Windows 非法文件名字符（\\ / : * ? " < > |），避免 regex 解析 {platform}_{account}.json
      出问题；正则本身允许 {account} 任意字符但文件名必须合法。
    - 超长（>64）→ 截断
    """
    import re as _re
    from tkinter import Tk
    from tkinter import simpledialog

    platform_name = _PLATFORM_NAMES.get(platform_key, platform_key)
    root = Tk()
    root.withdraw()
    # 为什么必须在 withdraw 之后 lift 一下：
    # tkinter 顶级窗口在简化对话框前不调用 lift 会在 Windows 上被其他窗口遮住（弹不出来最前）
    root.lift()
    root.attributes("-topmost", True)
    try:
        raw = simpledialog.askstring(
            title=f"登录 {platform_name}",
            prompt=f"请输入账号标识名（将保存为 {platform_key}_<标识名>.json，用于区分同平台多个账号）：",
            initialvalue="default",
            parent=root,
        )
    finally:
        root.destroy()

    if raw is None:
        # 用户点取消：不走登录流程（不打开浏览器）
        logger.info("prompt_account_and_login: %s 用户取消输入账号名，跳过登录", platform_key)
        return False

    # 过滤账号名
    cleaned = raw.strip() or "default"
    cleaned = _re.sub(r'[\\/:*?"<>|]', "_", cleaned)
    if len(cleaned) > 64:
        cleaned = cleaned[:64]

    logger.info("prompt_account_and_login: %s account=%s (用户输入=%s)", platform_key, cleaned, raw)
    return login_platform(platform_key, cleaned)
