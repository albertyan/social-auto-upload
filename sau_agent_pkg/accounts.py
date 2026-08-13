"""
sau_agent_pkg.accounts
~~~~~~~~~~~~~~~~~~~~~~
账号扫描与 cookie 有效性检查模块。

职责：
- 扫描 SAU_HOME/cookies/ 目录，解析 {platform}_{account}.json 文件名
- 调用 upstream_adapter.check_fns 验证 cookie 有效性
- 提供全量检查、单平台首个有效账号查询
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sau_agent_pkg.config import SAU_HOME
from sau_agent_pkg.upstream_adapter import check_fns

logger = logging.getLogger(__name__)

# cookies 目录
_COOKIES_DIR = SAU_HOME / "cookies"

# 文件名模式：{platform}_{account}.json
_COOKIE_PATTERN = re.compile(r"^([a-zA-Z0-9]+)_(.+)\.json$")


def scan() -> list[dict]:
    """
    扫描 SAU_HOME/cookies/ 目录，解析 {platform}_{account}.json 文件名。

    Returns:
        账号列表 [{"platform_key": str, "account_name": str}, ...]
    """
    accounts: list[dict] = []
    if not _COOKIES_DIR.exists():
        return accounts

    for f in _COOKIES_DIR.iterdir():
        if not f.is_file():
            continue
        m = _COOKIE_PATTERN.match(f.name)
        if m:
            accounts.append({
                "platform_key": m.group(1),
                "account_name": m.group(2),
            })
    return accounts


async def check_validity(platform: str, account: str) -> bool:
    """
    调用 upstream_adapter.check_fns[platform](account) 验证 cookie 有效性。

    Args:
        platform: 平台 key（如 "douyin"）
        account: 账号名

    Returns:
        cookie 是否有效
    """
    fn = check_fns.get(platform)
    if fn is None:
        logger.warning("Unknown platform for check: %s", platform)
        return False
    try:
        return await fn(account)
    except Exception:
        logger.exception("Error checking cookie for %s/%s", platform, account)
        return False


async def check_all() -> list[dict]:
    """
    全量检查所有账号的 cookie 有效性。

    Returns:
        [{platform_key, account_name, is_valid, checked_at}, ...]
    """
    accounts = scan()
    results: list[dict] = []
    for acc in accounts:
        is_valid = await check_validity(acc["platform_key"], acc["account_name"])
        results.append({
            "platform_key": acc["platform_key"],
            "account_name": acc["account_name"],
            "is_valid": is_valid,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        })
    return results


async def first_valid(platform: str) -> str | None:
    """
    返回该平台第一个有效账号名。

    Args:
        platform: 平台 key

    Returns:
        账号名，若无有效账号返回 None
    """
    accounts = scan()
    for acc in accounts:
        if acc["platform_key"] != platform:
            continue
        if await check_validity(platform, acc["account_name"]):
            return acc["account_name"]
    return None
