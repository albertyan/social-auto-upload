"""
sau_agent_pkg.accounts
~~~~~~~~~~~~~~~~~~~~~~
账号扫描与 cookie 有效性检查模块。

职责：
- 扫描 SAU_HOME/cookies/ 目录，解析 {platform}_{account}.json 文件名
- 同时兼容旧体系（Web 端登录）：SAU_HOME/cookiesFile/{uuid}.json + SQLite user_info 表
- 调用 upstream_adapter.check_fns（新体系）或 myUtils.auth.check_cookie（旧体系）验证 cookie
- 提供全量检查、单平台首个有效账号查询
- 全链路 timeout 兜底：单个账号检查上限 25s，整体 check_all 并发度可控
- 结果带状态缓存（last_result），避免用户刚点完立即刷新时重复检查
"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sau_agent_pkg.config import SAU_HOME
from sau_agent_pkg.db_init import DB_PATH as _DB_PATH
from sau_agent_pkg.upstream_adapter import check_fns

logger = logging.getLogger(__name__)

# cookies 目录（新体系：文件名 {platform}_{account}.json）
_COOKIES_DIR = SAU_HOME / "cookies"

# cookiesFile 目录 + SQLite（旧体系 Web 端登录：文件名是 UUID.json，元数据在 sau.db user_info 表）
# 为什么不直接用 SAU_HOME：myUtils/login.py / myUtils/auth.py 里写 SQLite 和 cookiesFile 的路径时，
# 全部使用 conf.BASE_DIR（而不是硬编码 SAU_HOME）。sau_tray 启动时 home_shim 会把 conf.BASE_DIR
# 改写为 SAU_HOME，但直接运行 python -m sau_agent_pkg.accounts 等脚本时不会应用垫片，此时
# conf.BASE_DIR 是项目根目录。因此兼容层必须优先取 conf.BASE_DIR，取不到再 fallback 到 SAU_HOME。
def _resolve_legacy_base() -> Path:
    """解析旧体系的 BASE_DIR，优先使用内存中已被 home_shim 重写过的 conf.BASE_DIR。"""
    try:
        import conf  # type: ignore[import-untyped]  # 与 myUtils/* 系列模块保持一致的 conf 引用
        base = getattr(conf, "BASE_DIR", None)
        if base is not None:
            return Path(base)
    except ImportError:
        pass
    return SAU_HOME


_LEGACY_BASE_DIR = _resolve_legacy_base()
_LEGACY_COOKIES_DIR = _LEGACY_BASE_DIR / "cookiesFile"
# 统一使用 sau.db（原 database.db 已迁移合并）
_LEGACY_DB_PATH = _DB_PATH

# 旧体系 user_info.type 数字 -> 新体系 platform_key 映射
# 为什么只有 4 个：myUtils/login.py 里 Web 端登录只实现了这 4 个平台（xiaohongshu/tencent/douyin/kuaishou），
# myUtils/auth.py 的 check_cookie() 也只对这 4 个数字写了分支（case 1/2/3/4），case _ 直接 return False。
# bilibili/baijiahao/youtube 只在新体系（sau_cli.login_* / cookies/ 目录）中存在，
# 旧体系 SQLite user_info 表根本不会写入 type=5/6/7 之类的记录，因此这里多写映射反而会造成误导。
_LEGACY_TYPE_TO_PLATFORM: dict[int, str] = {
    1: "xiaohongshu",
    2: "tencent",
    3: "douyin",
    4: "kuaishou",
}

# 文件名模式：{platform}_{account}.json
_COOKIE_PATTERN = re.compile(r"^([a-zA-Z0-9]+)_(.+)\.json$")

# 单账号检查最大耗时：为什么是 25s：
#   cookie_auth 内部会启动 patchright 浏览器 + page.goto 目标创作者页面，
#   国内创作者中心 DNS/CDN 偶尔抖动，10s 很容易撞时间窗口。
#   设 25s 既能覆盖 99% 的正常检查场景，又不会因一个账号慢阻塞整体。
_SINGLE_CHECK_TIMEOUT_SEC = 25

# check_all 的并发度：为什么不是 1 也不是更大：
#   每个检查都会启动独立浏览器进程（patchright chromium），
#   并发太高会吃满内存/CPU；3 路并发能兼顾整体耗时与机器开销。
_CHECK_ALL_CONCURRENCY = 3


# ---------------------------------------------------------------------------
# 检查结果缓存（最后一次全量检查结果）
# ---------------------------------------------------------------------------
@dataclass
class RecheckTask:
    task_id: str
    started_at_ms: int
    status: str = "running"  # running / done / failed
    total: int = 0
    done: int = 0
    accounts: list[dict] = field(default_factory=list)
    error: str | None = None


_last_check_lock = threading.Lock()
_last_check_result: dict[str, Any] = {}  # 键："{platform}|{account}"，值：检查结果 dict
_last_check_all: list[dict] | None = None  # 最后一次 check_all 的完整返回值
_last_check_all_time_ms: int = 0
_current_recheck: RecheckTask | None = None
_recheck_task_lock = threading.Lock()
_recheck_id_counter = 0


def _now_ms() -> int:
    return int(time.time() * 1000)


def set_last_check_cache(platform: str, account: str, entry: dict) -> None:
    """单个账号检查后更新细粒度缓存（first_valid 会受益）。"""
    with _last_check_lock:
        key = f"{platform}|{account}"
        _last_check_result[key] = entry


def get_last_check_all() -> list[dict] | None:
    """返回最后一次 check_all 结果（GUI 无需等待重跑就能显示最近一次检查）。"""
    return _last_check_all


def get_current_recheck() -> RecheckTask | None:
    """返回当前正在进行的 recheck 任务（若有）。"""
    with _recheck_task_lock:
        if _current_recheck is None:
            return None
        return RecheckTask(
            task_id=_current_recheck.task_id,
            started_at_ms=_current_recheck.started_at_ms,
            status=_current_recheck.status,
            total=_current_recheck.total,
            done=_current_recheck.done,
            accounts=list(_current_recheck.accounts),
            error=_current_recheck.error,
        )


def next_recheck_task_id() -> str:
    """生成下一次账号检查任务 id（线程安全）。"""
    with _recheck_task_lock:
        global _recheck_id_counter
        _recheck_id_counter += 1
        return f"recheck-{_recheck_id_counter}"


def _scan_new_style() -> list[dict]:
    """扫描新体系账号：SAU_HOME/cookies/{platform}_{account}.json。"""
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
                # legacy_type=None 表示是新体系账号，check_validity 会走 check_fns
                "legacy_type": None,
                "legacy_file": None,
            })
    return accounts


def _scan_legacy() -> list[dict]:
    """扫描旧体系账号：SAU_HOME/cookiesFile/{uuid}.json + SQLite user_info。

    为什么需要兼容旧体系：myUtils/login.py 登录产生的账号存在 cookiesFile/ + SQLite 中，
    如果不兼容，用户在 Web 端登录的账号在托盘「账号状态」弹框里完全看不到，
    会出现"Web 端有 3 个，托盘只显示 2 个"的一致性问题。
    """
    accounts: list[dict] = []
    # SQLite 或 cookiesFile 目录任一不存在：旧体系未使用过，直接返回空
    if not _LEGACY_DB_PATH.exists() or not _LEGACY_COOKIES_DIR.exists():
        return accounts
    try:
        conn = sqlite3.connect(str(_LEGACY_DB_PATH))
        try:
            cur = conn.cursor()
            # 表结构：id, type, filePath, userName, status, create_time ...
            # 只选 type, filePath, userName 三个就够；如果字段缺失失败就当旧体系空。
            try:
                cur.execute("SELECT type, filePath, userName FROM user_info")
            except Exception:
                return accounts
            for row in cur.fetchall():
                if len(row) < 3:
                    continue
                legacy_type, file_path, user_name = row[0], row[1], row[2]
                if legacy_type is None or file_path is None or user_name is None:
                    continue
                # 只在 cookiesFile 下真的有对应 UUID 文件时才认为账号有效
                cookie_file = _LEGACY_COOKIES_DIR / str(file_path)
                if not cookie_file.is_file():
                    logger.debug("scan legacy: skip missing cookie file type=%s user=%s file=%s",
                                 legacy_type, user_name, cookie_file)
                    continue
                platform = _LEGACY_TYPE_TO_PLATFORM.get(int(legacy_type))
                if platform is None:
                    logger.info("scan legacy: skip unknown legacy_type=%s user=%s", legacy_type, user_name)
                    continue
                accounts.append({
                    "platform_key": platform,
                    "account_name": str(user_name),
                    # legacy_type / legacy_file：check_validity 会用它们走 myUtils.auth.check_cookie
                    "legacy_type": int(legacy_type),
                    "legacy_file": str(file_path),
                })
        finally:
            conn.close()
    except Exception as e:
        # SQLite 打开/查询失败：当旧体系无账号处理（不抛错，扫描不因老库坏了整体废掉）
        logger.info("scan legacy: db query failed, skip legacy accounts error=%s", e)
    return accounts


# ---------------------------------------------------------------------------
# 扫描（新体系 + 旧体系合并，去重）
# ---------------------------------------------------------------------------
def scan() -> list[dict]:
    """
    扫描所有账号（新体系 SAU_HOME/cookies/ + 旧体系 SQLite/cookiesFile）。

    Returns:
        账号列表 [{"platform_key": str, "account_name": str,
                   "legacy_type": int|None, "legacy_file": str|None}, ...]
    """
    new_style = _scan_new_style()
    legacy = _scan_legacy()
    # 去重：同一 (platform_key, account_name) 同时存在两条记录时，保留新体系那条
    # 为什么优先新体系：新体系 cookie 文件路径约定更稳定，上传/登录流程走的都是新体系路径。
    seen: set[tuple[str, str]] = set()
    merged: list[dict] = []
    for acc in new_style:
        key = (acc["platform_key"], acc["account_name"])
        seen.add(key)
        merged.append(acc)
    for acc in legacy:
        key = (acc["platform_key"], acc["account_name"])
        if key in seen:
            logger.info("scan: skip dup legacy account %s/%s (new_style exists)",
                        acc["platform_key"], acc["account_name"])
            continue
        seen.add(key)
        merged.append(acc)
    if not merged:
        logger.info("scan: found 0 accounts (new=%d, legacy=%d)", len(new_style), len(legacy))
    else:
        logger.info("scan: done total=%d (new_style=%d legacy=%d)",
                    len(merged), len(new_style), len(legacy))
    return merged


# ---------------------------------------------------------------------------
# 单账号检查
# ---------------------------------------------------------------------------
async def _check_legacy_account(legacy_type: int, legacy_file: str) -> bool:
    """检查旧体系账号 cookie 有效性：转发到 myUtils.auth.check_cookie(type, fileName)。

    为什么要单独封装：
    - myUtils.auth 是老模块，import 可能失败（环境不完整时），这里单独 try/except 隔离
    - check_cookie(type, fileName) 的参数约定和新体系 check_fns 不同，单独一层便于维护
    """
    try:
        from myUtils.auth import check_cookie as legacy_check_cookie
    except Exception as e:
        logger.warning("_check_legacy_account: import myUtils.auth.check_cookie failed, error=%s", e)
        return False
    try:
        # check_cookie(type: int, fileName: str) → 读取 SAU_HOME/cookiesFile/{fileName}
        return bool(await legacy_check_cookie(legacy_type, legacy_file))
    except Exception as e:
        logger.warning(
            "_check_legacy_account: check_cookie(type=%s, file=%s) exception=%s: %s",
            legacy_type, legacy_file, type(e).__name__, e,
        )
        return False


async def check_validity(platform: str, account: str, **extra) -> bool:
    """
    验证 cookie 有效性（兼容新/旧两套体系）。

    单账号超时 25s 兜底：超时按 False 处理，避免慢账号拖死整个调用链。

    Args:
        platform: 平台 key（如 "douyin"）
        account: 账号名
        **extra: 可选扩展字段：legacy_type / legacy_file（表示是旧体系账号）

    Returns:
        cookie 是否有效
    """
    # 单账号开始 debug：开发/排查时能知道某个账号检查有没有真的启动，避免上层没调用到的假象
    legacy_type = extra.get("legacy_type") if extra else None
    legacy_file = extra.get("legacy_file") if extra else None
    logger.debug(
        "check_validity: start platform=%s account=%s legacy_type=%s legacy_file=%s",
        platform, account, legacy_type, legacy_file,
    )
    start_ms = _now_ms()
    try:
        # 外层 asyncio.wait_for 作为总闸：即使 cookie_auth 内部 goto/wait_for 没设
        # timeout，这里也会强杀任务，保证单账号不会无限阻塞。
        if legacy_type is not None and legacy_file is not None:
            # 旧体系账号：走 myUtils.auth.check_cookie(type, fileName)
            is_valid = await asyncio.wait_for(
                _check_legacy_account(int(legacy_type), str(legacy_file)),
                timeout=_SINGLE_CHECK_TIMEOUT_SEC,
            )
        else:
            # 新体系账号：走 upstream_adapter.check_fns[platform](account)
            fn = check_fns.get(platform)
            if fn is None:
                logger.warning("Unknown platform for check: %s", platform)
                return False
            is_valid = await asyncio.wait_for(
                fn(account), timeout=_SINGLE_CHECK_TIMEOUT_SEC,
            )
        return bool(is_valid)
    except asyncio.TimeoutError:
        elapsed_ms = _now_ms() - start_ms
        # 超时 warning 带 platform+account+耗时：一眼看出是哪条账号卡死了，耗时对比 25s 阈值
        logger.warning(
            "check_validity: timed out (%ss) for %s/%s, elapsed=%dms, treat as invalid",
            _SINGLE_CHECK_TIMEOUT_SEC, platform, account, elapsed_ms,
        )
        return False
    except Exception as e:
        elapsed_ms = _now_ms() - start_ms
        # 异常 warning 带 platform+account+耗时：区分是超时还是浏览器崩溃/网络异常
        logger.warning(
            "check_validity: exception for %s/%s, elapsed=%dms, error=%s: %s",
            platform, account, elapsed_ms, type(e).__name__, e,
        )
        return False


async def _check_one_and_record(
    platform: str,
    account: str,
    sem: asyncio.Semaphore | None = None,
    progress_cb=None,
    legacy_type: int | None = None,
    legacy_file: str | None = None,
) -> dict:
    """封装 check_validity + 更新缓存 + 可选进度回调。"""
    start_ms = _now_ms()
    if sem is not None:
        await sem.acquire()
    try:
        is_valid = await check_validity(
            platform, account,
            legacy_type=legacy_type, legacy_file=legacy_file,
        )
        elapsed_ms = _now_ms() - start_ms
        # 每完成 1 个 info 摘要 is_valid+耗时：批量检查时能看到整体推进速度，异常账号耗时会明显偏高
        logger.info(
            "_check_one_and_record: done platform=%s account=%s is_valid=%s elapsed=%dms legacy_type=%s",
            platform, account, is_valid, elapsed_ms, legacy_type,
        )
        entry = {
            "platform_key": platform,
            "account_name": account,
            "is_valid": is_valid,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "legacy_type": legacy_type,
            "legacy_file": legacy_file,
        }
        set_last_check_cache(platform, account, entry)
        if progress_cb is not None:
            try:
                progress_cb(entry)
            except Exception:
                logger.exception("progress_cb 抛错，忽略")
        return entry
    finally:
        if sem is not None:
            sem.release()


# ---------------------------------------------------------------------------
# 全量检查：并发 3 路 + 结果缓存 + 进度追踪（供后台任务模式使用）
# ---------------------------------------------------------------------------
async def check_all() -> list[dict]:
    """
    全量检查所有账号的 cookie 有效性（新体系 + 旧体系账号）。
    每个账号并发检查（并发度 3，受限于浏览器进程数）；
    结果写入全局缓存，后续 /status 或菜单查看无需重跑。

    Returns:
        [{platform_key, account_name, is_valid, checked_at, legacy_type, legacy_file}, ...]
    """
    accounts = scan()
    total = len(accounts)
    start_ms = _now_ms()
    # 开始 info 含总数：运维能对照预期账号数，确认扫描是否漏扫
    logger.info("check_all: start, total accounts=%d", total)
    sem = asyncio.Semaphore(_CHECK_ALL_CONCURRENCY)

    tasks = [
        asyncio.create_task(_check_one_and_record(
            acc["platform_key"], acc["account_name"], sem=sem,
            legacy_type=acc.get("legacy_type"),
            legacy_file=acc.get("legacy_file"),
        ))
        for acc in accounts
    ]
    results = []
    if tasks:
        done, _ = await asyncio.wait(tasks)
        for fut in done:
            try:
                results.append(fut.result())
            except Exception:
                # _check_one_and_record 内部已经 swallow 了大部分异常，
                # 这里兜 Task 被取消/抛未捕获异常时不会让整个 check_all 中断。
                logger.exception("check_all 单任务异常，按无效处理")
    # 按账号名排序，保证展示稳定
    results.sort(key=lambda r: (r["platform_key"], r["account_name"]))
    global _last_check_all, _last_check_all_time_ms
    with _last_check_lock:
        _last_check_all = results
        _last_check_all_time_ms = _now_ms()
    valid_count = sum(1 for r in results if r.get("is_valid"))
    invalid_count = len(results) - valid_count
    elapsed_ms = _now_ms() - start_ms
    # 结束 info 含分布 N 有效/M 失效，整体耗时：运维能快速评估账号健康度和批次耗时
    logger.info(
        "check_all: done, valid=%d invalid=%d total=%d elapsed=%dms",
        valid_count, invalid_count, total, elapsed_ms,
    )
    return results


async def run_recheck_background(task_id: str) -> None:
    """后台异步版本 check_all，带进度写入 current_recheck（local_api 用）。"""
    start_ms = _now_ms()
    # 开始 info 含 task_id：GUI 侧提交的 task_id 和后台实际执行能关联起来
    logger.info("run_recheck_background: start task_id=%s", task_id)
    accounts = scan()
    total = len(accounts)
    task = RecheckTask(
        task_id=task_id,
        started_at_ms=_now_ms(),
        status="running",
        total=total,
        done=0,
        accounts=[],
    )
    with _recheck_task_lock:
        global _current_recheck
        _current_recheck = task

    sem = asyncio.Semaphore(_CHECK_ALL_CONCURRENCY)

    def _on_progress(entry: dict) -> None:
        with _recheck_task_lock:
            cur = _current_recheck
            if cur is None:
                return
            cur.accounts.append(entry)
            cur.done += 1

    try:
        tasks = [
            asyncio.create_task(_check_one_and_record(
                acc["platform_key"], acc["account_name"],
                sem=sem, progress_cb=_on_progress,
                legacy_type=acc.get("legacy_type"),
                legacy_file=acc.get("legacy_file"),
            ))
            for acc in accounts
        ]
        if tasks:
            done, pending = await asyncio.wait(tasks)
            # 以防万一 pending 不是空（极端情况下 wait 提前返回），一律取消
            for p in pending:
                p.cancel()
            for fut in done:
                try:
                    fut.result()
                except Exception:
                    logger.exception("run_recheck_background 单任务异常，按无效")
    except Exception as exc:
        with _recheck_task_lock:
            if _current_recheck is not None:
                _current_recheck.status = "failed"
                _current_recheck.error = f"{type(exc).__name__}: {exc}"
        # 异常 info 含 task_id：知道是哪次后台任务炸了，便于排查
        logger.info("run_recheck_background: exception task_id=%s elapsed=%dms error=%s", task_id, _now_ms() - start_ms, exc)
        return

    # 收尾：标记 done + 写入全局 last_check_all
    final_results: list[dict] = []
    with _recheck_task_lock:
        if _current_recheck is not None:
            _current_recheck.status = "done"
            final_results = list(_current_recheck.accounts)
    final_results.sort(key=lambda r: (r["platform_key"], r["account_name"]))
    global _last_check_all, _last_check_all_time_ms
    with _last_check_lock:
        _last_check_all = final_results
        _last_check_all_time_ms = _now_ms()
    # 结束 info 含 task_id：能和开始日志配对，确认执行完整走完
    logger.info("run_recheck_background: done task_id=%s elapsed=%dms total=%d", task_id, _now_ms() - start_ms, total)


# ---------------------------------------------------------------------------
# 单平台首个有效账号
# ---------------------------------------------------------------------------
async def first_valid(platform: str) -> str | None:
    """
    返回该平台第一个有效账号名（兼容旧体系账号）。

    Args:
        platform: 平台 key

    Returns:
        账号名，若无有效账号返回 None
    """
    accounts = scan()
    for acc in accounts:
        if acc["platform_key"] != platform:
            continue
        # 逐账号试 debug：没命中缓存时能看到 first_valid 在一个个尝试哪些账号
        logger.debug("first_valid: trying platform=%s account=%s", platform, acc["account_name"])
        # 命中最近一次缓存且 is_valid=True 时直接返回，少跑一次浏览器
        with _last_check_lock:
            cache = _last_check_result.get(f"{platform}|{acc['account_name']}")
        if cache and cache.get("is_valid"):
            checked_at = cache.get("checked_at") or ""
            # 缓存 10 分钟内认为有效（cookie 不会在 10 分钟内戏剧性地失效）
            try:
                dt = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
                if (_now_ms() - int(dt.timestamp() * 1000)) < 10 * 60 * 1000:
                    # 命中缓存 info 带 account：能看到调度器用了缓存没启浏览器，省了排查时间
                    logger.info("first_valid: cache hit platform=%s account=%s", platform, acc["account_name"])
                    return acc["account_name"]
            except Exception:
                pass
        if await check_validity(
            platform, acc["account_name"],
            legacy_type=acc.get("legacy_type"),
            legacy_file=acc.get("legacy_file"),
        ):
            return acc["account_name"]
    return None
