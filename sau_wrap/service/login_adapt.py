# -*- coding: utf-8 -*-
"""抖音登录短信二验运行时内存级适配（S9；铁律：上游文件零修改）。

**背景（上游缺口）**：上游 ``uploader/douyin_uploader/main.py`` 的
``_wait_for_douyin_login`` 检测到短信/安全二次验证输入框后仅记日志，等待
**有头浏览器中的手动输入**——服务进程 headless（Session 0）下不可用。

**适配方式（运行时内存级，上游文件零修改）**：登录会话执行期间把上游
模块属性 ``_wait_for_douyin_login`` 替换为带验证码注入通道的适配版
（上游 ``douyin_cookie_gen`` 以模块全局名引用该函数，替换模块属性即生效），
会话结束（含异常）经上下文管理器还原原函数。

适配版等待循环与上游逻辑对齐（登录完成判定 / 二维码失效刷新均复用上游
模块函数，只读引用），仅把「检测到短信输入框」分支从「记日志等手动输入」
改为：会话置 ``need_input`` → 等待控制台 ``POST /login/{session_id}/code``
注入（单次窗口 120s，超时/会话终态 → 本次登录 failed）→ 填入并尝试提交
（优先候选确认按钮「验证/确定/确认/登录/提交」，兜底回车）→ 继续等待登录完成。

**验证边界**：短信二验为平台侧低频触发路径，本适配以假登录器验证协议链路
（``tests/verify_s9.py``），真实页面注入效果待真机回归（见 README 遗留清单）。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

#: 上游短信输入框选择器（与上游 _wait_for_douyin_login / 发布流程同源）
_SMS_INPUT_SELECTOR = (
    'input[placeholder*="验证码"], input[type="tel"], '
    'input[placeholder*="短信"], input[placeholder*="手机号"]'
)

#: 验证码注入单次等待窗口（秒）；会话总超时（5 分钟）仍由管理器兜底
_CODE_WAIT_TIMEOUT = 120.0


@asynccontextmanager
async def douyin_sms_bridge(session):
    """会话期替换上游等待函数（结束必还原，含异常路径）。"""
    from uploader.douyin_uploader import main as dm  # noqa: PLC0415

    original = dm._wait_for_douyin_login
    dm._wait_for_douyin_login = _make_adapted_wait(session, dm)
    try:
        yield
    finally:
        dm._wait_for_douyin_login = original


def _make_adapted_wait(session, dm):
    """构造适配版等待函数（签名与上游一致，上游以关键字参数调用）。"""

    async def _wait_for_douyin_login_adapted(
            page, account_file, qrcode_info, qrcode_callback=None,
            poll_interval: int = 3, max_checks: int = 100) -> dict:
        # —— 以下主循环结构与上游 _wait_for_douyin_login 对齐（只读参照）——
        qrcode_path = (Path(qrcode_info["image_path"])
                       if qrcode_info.get("image_path") else None)
        original_url = page.url
        for _ in range(max_checks):
            if await dm._is_douyin_login_completed(page):
                return dm._build_login_result(
                    True, "success", "抖音扫码登录成功",
                    account_file, qrcode_info, page.url)

            # URL 变化 + sessionid 未到位 → 二验流程
            if page.url != original_url and not await dm._is_douyin_login_completed(page):
                sms_input = page.locator(_SMS_INPUT_SELECTOR)
                if await sms_input.count() > 0:
                    code = await _request_and_inject_code(session, page, sms_input)
                    if code is None:
                        return dm._build_login_result(
                            False, "failed",
                            "抖音短信二验：验证码等待超时或会话已终态",
                            account_file, qrcode_info, page.url)
                    # 注入后回等待态，继续轮询登录完成判定
                await asyncio.sleep(poll_interval)
                continue

            # 二维码失效刷新（与上游一致：点击刷新 → 重新提取 → 再回调）
            expired_box = page.get_by_text("二维码失效", exact=True).locator("..").first
            if await expired_box.count() and await expired_box.is_visible():
                await expired_box.click()
                await asyncio.sleep(1)
                qrcode_info = await dm._save_douyin_qrcode(
                    page, account_file, qrcode_path, qrcode_callback=qrcode_callback)
                qrcode_path = (Path(qrcode_info["image_path"])
                               if qrcode_info.get("image_path") else None)

            await asyncio.sleep(poll_interval)

        return dm._build_login_result(
            False, "timeout", "等待抖音扫码登录超时",
            account_file, qrcode_info, page.url)

    return _wait_for_douyin_login_adapted


async def _request_and_inject_code(session, page, sms_input) -> str | None:
    """置 need_input → 等注入 → 填入并提交。超时/会话终态返回 None。"""
    session.message = "抖音短信二次验证：请在控制台输入收到的验证码"
    try:
        code = await session.wait_for_code(timeout=_CODE_WAIT_TIMEOUT)
    except asyncio.TimeoutError:
        session.message = "短信验证码等待超时"
        return None
    code = (code or "").strip()
    if not code:
        return None
    first = sms_input.first
    try:
        await first.fill(code)
    except Exception:  # noqa: BLE001
        return None
    # 提交：优先尝试常见确认按钮，兜底回车（真机回归前的保守策略）
    for name in ("验证", "确定", "确认", "登录", "提交"):
        try:
            btn = page.get_by_role("button", name=name).first
            if await btn.count() and await btn.is_visible():
                await btn.click()
                return code
        except Exception:  # noqa: BLE001
            continue
    try:
        await first.press("Enter")
    except Exception:  # noqa: BLE001
        pass
    return code
