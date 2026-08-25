# -*- coding: utf-8 -*-
"""账号快照（S4：注册/心跳/``account_sync`` 上行的 ``accounts`` 字段）。

- 主目录：``%ProgramData%\\SAU\\cookies\\{platform}_{account}.json``（§3.6 定案布局）；
- **兼容目录**：上游仓库内 ``cookies/``——上游 ``sau_cli.py <platform> login`` 默认把
  cookie 写到 ``conf.BASE_DIR/cookies``（仓库内），包装层只读消费、绝不修改上游；
  同名 ``{platform}_{account}`` 以主目录优先；
- 本步 ``is_valid`` 语义：cookie 文件存在且为合法 JSON 即视为可用（真实浏览器
  有效性复核需 patchright，属后续步骤 ``/accounts/recheck`` 范围）。
"""

from __future__ import annotations

import json
from pathlib import Path

from sau_wrap import paths

#: 上游仓库内 cookies 目录（sau_wrap 位于仓库根下一层 → parents[2]）
UPSTREAM_COOKIES_DIR: Path = Path(__file__).resolve().parents[2] / "cookies"

#: 受支持的平台注册表（与 upstream_adapter 映射表一致；tencent 即视频号，
#: 服务端落库时自行反向转 shipinhao，客户端一律用 tencent）
PLATFORM_KEYS = (
    "douyin", "kuaishou", "xiaohongshu", "bilibili", "tencent",
    "youtube", "baijiahao",
)


def cookie_dirs(extra: list[Path] | None = None) -> list[Path]:
    """扫描目录优先级：主目录 > 兼容目录 > 额外注入（测试用）。"""
    dirs = [paths.COOKIES_DIR]
    if UPSTREAM_COOKIES_DIR.is_dir():
        dirs.append(UPSTREAM_COOKIES_DIR)
    if extra:
        dirs.extend(extra)
    return dirs


def scan_accounts(extra_dirs: list[Path] | None = None) -> list[dict]:
    """扫描 cookies 目录生成账号快照（§3.3 register/heartbeat/account_sync 字段）。

    返回元素：``{"platform_key", "account_name", "is_valid", "source"}``；
    文件名为 ``{platform}_{account}.json``（platform 限注册表内取值）。
    """
    seen: dict[str, dict] = {}
    for index, base in enumerate(cookie_dirs(extra_dirs)):
        if not base.is_dir():
            continue
        source = "primary" if index == 0 else "fallback"
        for f in sorted(base.glob("*.json")):
            stem = f.stem
            platform = next(
                (p for p in PLATFORM_KEYS if stem.startswith(p + "_")), None
            )
            if platform is None:
                continue
            account = stem[len(platform) + 1:]
            if not account:
                continue
            key = f"{platform}_{account}"
            if key in seen:
                continue  # 主目录优先
            is_valid = _looks_valid(f)
            seen[key] = {
                "platform_key": platform,
                "account_name": account,
                "is_valid": is_valid,
                "source": source,
            }
    return sorted(seen.values(), key=lambda a: (a["platform_key"], a["account_name"]))


def find_account_file(
    platform_key: str, account_name: str, extra_dirs: list[Path] | None = None
) -> Path | None:
    """按名定位 cookie 文件（供适配层传给上游上传器）。"""
    name = f"{platform_key}_{account_name}.json"
    for base in cookie_dirs(extra_dirs):
        candidate = base / name
        if candidate.is_file():
            return candidate
    return None


def resolve_account(
    platform_key: str,
    account_name: str | None,
    extra_dirs: list[Path] | None = None,
) -> dict | None:
    """账号解析（§5.3）：指定 ``account_name`` 按名取，否则 first_valid。"""
    snapshot = scan_accounts(extra_dirs)
    candidates = [a for a in snapshot if a["platform_key"] == platform_key]
    if account_name:
        picked = next(
            (a for a in candidates if a["account_name"] == account_name), None
        )
        return picked if picked and picked["is_valid"] else None
    return next((a for a in candidates if a["is_valid"]), None)


def _looks_valid(path: Path) -> bool:
    """cookie 文件存在且为合法 JSON（playwright storage_state 格式）。"""
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except (OSError, ValueError):
        return False
