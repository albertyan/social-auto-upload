"""
sau_agent_pkg.accounts
~~~~~~~~~~~~~~~~~~~~~~
账号扫描与 cookie 有效性检查模块（按职责拆分为多个小类）。

主要职责：
- 扫描 SAU_HOME/cookies/ 目录（新体系），解析 {platform}_{account}.json 文件名
- 兼容旧体系（Web 端登录）：SAU_HOME/cookiesFile/{uuid}.json + SQLite user_info 表
- 调用 upstream_adapter.check_fns（新体系）或 myUtils.auth.check_cookie（旧体系）验证 cookie
- 提供全量检查、单平台首个有效账号查询
- 提供「账号变更事件」回调钩子：监听 cookie 文件增删改 + 检查结果变化
  （供 SauAgentCore 实时上送 account_sync 消息给上游）

为什么要拆成多个类：
- 原模块是单文件大集合（591 行，混着扫描/检查/任务管理/FS 监听/旧体系兼容），
  修改其中一个功能容易误碰别的。按领域拆成 Scanner / Checker / Manager 三类后，
  每个类职责单一，代码可读性与可维护性更高。
- 保持向后兼容：对外 scan() / check_all() / check_validity() / first_valid() /
  get_last_check_all() / get_current_recheck() / next_recheck_task_id() /
  run_recheck_background() / set_last_check_cache() 9 个顶层函数签名全部不变，
  上游 local_api / dispatcher / tray_app 无需改代码。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sau_agent_pkg.config import SAU_HOME
from sau_agent_pkg.db_init import DB_PATH as _DB_PATH
from sau_agent_pkg.upstream_adapter import check_fns

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量（放在模块顶部，便于统一调参）
# ---------------------------------------------------------------------------

# cookies 目录（新体系：文件名 {platform}_{account}.json）
_COOKIES_DIR = SAU_HOME / "cookies"

# 文件名模式：{platform}_{account}.json
# platform 只允许字母数字（上游 7 个平台 key 都是），account 部分任意字符
_COOKIE_PATTERN = re.compile(r"^([a-zA-Z0-9]+)_(.+)\.json$")

# cookiesFile 目录 + SQLite（旧体系 Web 端登录）
# 为什么要独立解析 conf.BASE_DIR：myUtils/login.py / myUtils/auth.py 读写 SQLite 和
# cookiesFile 时全部用 conf.BASE_DIR；sau_tray 启动时 home_shim 会改写它，
# 但 CLI 单独跑 accounts 模块时垫片没生效，这里必须优先用内存里已改写的 BASE_DIR
def _resolve_legacy_base() -> Path:
    """解析旧体系的 BASE_DIR，优先取 home_shim 重写过的 conf.BASE_DIR。"""
    try:
        import conf  # type: ignore[import-untyped]
        base = getattr(conf, "BASE_DIR", None)
        if base is not None:
            return Path(base)
    except ImportError:
        pass
    return SAU_HOME


_LEGACY_BASE_DIR = _resolve_legacy_base()
_LEGACY_COOKIES_DIR = _LEGACY_BASE_DIR / "cookiesFile"
_LEGACY_DB_PATH = _DB_PATH  # 原 database.db 已迁移合并进统一 sau.db

# 旧体系 user_info.type 数字 -> 新体系 platform_key 映射
# 为什么只有 4 个：myUtils/login.py 里 Web 端登录只实现了 1/2/3/4 四个类型，
# myUtils/auth.check_cookie() 也只对这 4 个数字写了分支，其他直接 return False。
# B站/百家号/YouTube 走新体系，旧体系 SQLite 不会写入 type=5/6/7。
_LEGACY_TYPE_TO_PLATFORM: dict[int, str] = {
    1: "xiaohongshu",
    2: "tencent",
    3: "douyin",
    4: "kuaishou",
}

# 单账号检查最大耗时（25s 覆盖 99% 正常场景，又不会被单个慢账号拖死整体）
_SINGLE_CHECK_TIMEOUT_SEC = 25
# 全量检查并发度：每账号会起独立浏览器进程，3 路兼顾耗时和机器开销
_CHECK_ALL_CONCURRENCY = 3
# 首次 last_check 缓存命中有效期：10 分钟内不再重复启动浏览器
_CACHE_TTL_MS = 10 * 60 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# EventLoop Policy 切换（旧体系 myUtils.auth 用）
# ---------------------------------------------------------------------------
# 为什么本地独立一份：upstream_adapter 已经有一份同名管理器，但如果 accounts.py
# 从上游 import 会形成"upstream_adapter 导入期间尝试 import accounts"的间接循环，
# 这里逻辑只有十几行，独立抄一份更安全。
@contextlib.contextmanager
def _proactor_policy_for_subprocess_local():
    """Windows 下临时切到 ProactorEventLoopPolicy（子进程/Playwright 需要）。"""
    if sys.platform != "win32":
        yield
        return
    current_policy = asyncio.get_event_loop_policy()
    try:
        proactor_cls = asyncio.WindowsProactorEventLoopPolicy  # type: ignore[attr-defined]
    except AttributeError:
        yield
        return
    if isinstance(current_policy, proactor_cls):
        yield  # 外层 upstream_adapter 已切过，不用再切
        return
    asyncio.set_event_loop_policy(proactor_cls())
    logger.debug("proactor_policy_local: 切换为 ProactorPolicy（旧体系检查用）")
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(current_policy)
        logger.debug("proactor_policy_local: 恢复原 EventLoopPolicy")


# ===================================================================
# 数据结构
# ===================================================================
@dataclass
class RecheckTask:
    """后台 recheck 任务追踪对象（local_api GET /accounts/status?task_id=xxx 用）。"""
    task_id: str
    started_at_ms: int
    status: str = "running"       # running / done / failed
    total: int = 0
    done: int = 0
    accounts: list[dict] = field(default_factory=list)
    error: str | None = None


@dataclass
class AccountEvent:
    """账号变更事件（Scanner/Checker 产生，回调给上层上送 account_sync）。"""
    source: str          # "scanner"（文件增删改） / "checker"（有效性变化）
    action: str          # "added" / "removed" / "changed" / "validity_switched"
    platform: str
    account: str
    # 对 source=checker 时提供有效性变化；source=scanner 时 is_valid=None（未检查）
    is_valid: bool | None = None
    checked_at: str | None = None
    legacy_type: int | None = None
    legacy_file: str | None = None


# ===================================================================
# 账号管理中心事件总线（跨类共享的回调钩子容器）
# ===================================================================
class AccountEventBus:
    """账号增删改 + 有效性变化事件回调注册表。

    为什么单独做一个类：
    - Scanner、ValidityChecker、RecheckTaskManager 三类都可能要触发事件；
    - 顶层入口（scan/check_validity/run_recheck_background）不需要知道事件发给谁，
      只负责 publish；谁关心谁 subscribe。
    - SauAgentCore 在启动时 subscribe(lambda e: 上送 account_sync)，
      托盘/CLI 也可以 subscribe 做本地 UI 刷新。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._listeners: list[Callable[[AccountEvent], None]] = []

    def subscribe(self, cb: Callable[[AccountEvent], None]) -> Callable[[], None]:
        """订阅事件；返回取消订阅的 callable。"""
        with self._lock:
            self._listeners.append(cb)
        _idx = len(self._listeners) - 1

        def _unsub() -> None:
            with self._lock:
                if 0 <= _idx < len(self._listeners) and self._listeners[_idx] is cb:
                    self._listeners.pop(_idx)

        return _unsub

    def publish(self, event: AccountEvent) -> None:
        """向所有监听器广播事件；任何单个 listener 抛错不影响后续。"""
        with self._lock:
            listeners_snapshot = list(self._listeners)
        for cb in listeners_snapshot:
            try:
                cb(event)
            except Exception:
                logger.exception("AccountEventBus listener 抛错已忽略：%s", getattr(cb, "__name__", cb))


# 全局唯一事件总线（所有类共享；顶层入口 publish，上层入口 subscribe）
_EVENT_BUS = AccountEventBus()


def subscribe_account_events(cb: Callable[[AccountEvent], None]) -> Callable[[], None]:
    """顶层订阅入口（供 SauAgentCore / tray_app 调用）。"""
    return _EVENT_BUS.subscribe(cb)


# ===================================================================
# Scanner 类：扫描新体系 cookies 目录 + 旧体系 SQLite，合并去重
# ===================================================================
class AccountScanner:
    """账号文件扫描器（纯数据读取 + 去重，不做浏览器检查）。

    职责：
    - 扫描 SAU_HOME/cookies/{platform}_{account}.json（新体系）
    - 扫描 SAU_HOME/cookiesFile/*.json + user_info 表（旧体系）
    - 按 (platform_key, account_name) 去重，同键冲突时新体系优先
    - 维护「上一次快照」，外部调用 diff_and_emit() 时产出 AccountEvent（增/删/改）
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_snapshot: dict[tuple[str, str], dict] = {}

    # ---------------------------------------------------------------
    # 各体系单独扫描
    # ---------------------------------------------------------------
    def _scan_new_style(self) -> list[dict]:
        """扫描新体系：SAU_HOME/cookies/{platform}_{account}.json。"""
        results: list[dict] = []
        if not _COOKIES_DIR.exists():
            return results
        for f in _COOKIES_DIR.iterdir():
            if not f.is_file():
                continue
            m = _COOKIE_PATTERN.match(f.name)
            if not m:
                continue
            results.append({
                "platform_key": m.group(1),
                "account_name": m.group(2),
                "legacy_type": None,
                "legacy_file": None,
                "_mtime_ms": int(f.stat().st_mtime * 1000) if f.exists() else 0,
            })
        return results

    def _scan_legacy(self) -> list[dict]:
        """扫描旧体系：cookiesFile/{uuid}.json + SQLite user_info。

        为什么扫描过程 SQLite/cookiesFile 任一不存在就返回空：
        旧体系只有 Web 端登录过才会同时写入两者；缺任一说明旧体系没被初始化过，
        不必再做后续 IO，直接返回空减少开销。
        """
        results: list[dict] = []
        if not _LEGACY_DB_PATH.exists() or not _LEGACY_COOKIES_DIR.exists():
            return results
        try:
            conn = sqlite3.connect(str(_LEGACY_DB_PATH))
            try:
                cur = conn.cursor()
                # 表结构：id, type, filePath, userName, status, create_time, ...
                # 只取 type/filePath/userName 三列，缺列时 try/except 吞掉返回空
                try:
                    cur.execute("SELECT type, filePath, userName FROM user_info")
                except Exception:
                    return results
                for row in cur.fetchall():
                    if len(row) < 3:
                        continue
                    legacy_type, file_path, user_name = row[0], row[1], row[2]
                    if legacy_type is None or file_path is None or user_name is None:
                        continue
                    cookie_file = _LEGACY_COOKIES_DIR / str(file_path)
                    if not cookie_file.is_file():
                        logger.debug(
                            "scan_legacy: skip missing cookie type=%s user=%s file=%s",
                            legacy_type, user_name, cookie_file,
                        )
                        continue
                    platform = _LEGACY_TYPE_TO_PLATFORM.get(int(legacy_type))
                    if platform is None:
                        logger.info("scan_legacy: skip unknown legacy_type=%s user=%s", legacy_type, user_name)
                        continue
                    mtime_ms = int(cookie_file.stat().st_mtime * 1000) if cookie_file.exists() else 0
                    results.append({
                        "platform_key": platform,
                        "account_name": str(user_name),
                        "legacy_type": int(legacy_type),
                        "legacy_file": str(file_path),
                        "_mtime_ms": mtime_ms,
                    })
            finally:
                conn.close()
        except Exception as e:
            logger.info("scan_legacy: db read failed, skip legacy accounts error=%s", e)
        return results

    # ---------------------------------------------------------------
    # 对外主方法
    # ---------------------------------------------------------------
    def scan(self, emit_events: bool = True) -> list[dict]:
        """扫描所有账号（新 + 旧，去重）。

        Args:
            emit_events: 是否对比上一次快照并 publish added/removed/changed 事件。
                         首次调用（或 _last_snapshot 空）不会发 added，避免启动时
                         被误认为是真实的"新增"。

        Returns:
            账号列表（去掉内部辅助字段 _mtime_ms）。
        """
        new_style = self._scan_new_style()
        legacy = self._scan_legacy()
        # 去重：同 (p, a) 复合键，new_style 优先
        seen: set[tuple[str, str]] = set()
        merged: dict[tuple[str, str], dict] = {}
        for acc in new_style:
            key = (acc["platform_key"], acc["account_name"])
            seen.add(key)
            merged[key] = acc
        for acc in legacy:
            key = (acc["platform_key"], acc["account_name"])
            if key in seen:
                logger.info("scan: skip dup legacy account %s/%s (new_style exists)",
                            acc["platform_key"], acc["account_name"])
                continue
            seen.add(key)
            merged[key] = acc

        snapshot_now = merged
        # 先准备对外的返回（去掉内部辅助字段）
        out = []
        for rec in snapshot_now.values():
            cleaned = {k: v for k, v in rec.items() if not k.startswith("_")}
            out.append(cleaned)
        out.sort(key=lambda r: (r["platform_key"], r["account_name"]))

        # 与上次快照比较，产出事件
        if emit_events:
            self._diff_and_emit_locked(snapshot_now)

        logger.info("scan: done total=%d new_style=%d legacy=%d",
                    len(out), len(new_style), len(legacy))
        return out

    # ---------------------------------------------------------------
    # 内部：快照 diff + 事件发布
    # ---------------------------------------------------------------
    def _diff_and_emit_locked(self, snapshot_now: dict[tuple[str, str], dict]) -> None:
        with self._lock:
            last = self._last_snapshot
            is_first = not last
            # 构造下一次要用的快照（保留 _mtime_ms 辅助字段，便于下一次 changed 判定）
            next_snapshot: dict[tuple[str, str], dict] = {k: dict(v) for k, v in snapshot_now.items()}
            self._last_snapshot = next_snapshot
            if is_first:
                # 首次扫描只记录快照，不 emit 事件，避免启动时把"已经在磁盘上"的账号
                # 都当做 added 触发一次 account_sync 全量
                return

        added: list[tuple[str, str]] = []
        removed: list[tuple[str, str]] = []
        changed: list[tuple[str, str]] = []

        all_keys = set(last.keys()) | set(snapshot_now.keys())
        for k in all_keys:
            p, a = k
            if k in last and k not in snapshot_now:
                removed.append((p, a))
            elif k not in last and k in snapshot_now:
                added.append((p, a))
            else:
                # 都存在时，用 _mtime_ms 判断是否"文件被覆盖/替换"（视为 changed）
                prev_mtime = last[k].get("_mtime_ms") or 0
                curr_mtime = snapshot_now[k].get("_mtime_ms") or 0
                if prev_mtime != curr_mtime:
                    changed.append((p, a))

        for p, a in added:
            _EVENT_BUS.publish(AccountEvent(source="scanner", action="added", platform=p, account=a))
        for p, a in removed:
            _EVENT_BUS.publish(AccountEvent(source="scanner", action="removed", platform=p, account=a))
        for p, a in changed:
            _EVENT_BUS.publish(AccountEvent(source="scanner", action="changed", platform=p, account=a))

        if added or removed or changed:
            logger.info("scan diff: added=%d removed=%d changed=%d", len(added), len(removed), len(changed))


# 全局扫描器（顶层入口共享，保证 _last_snapshot 连续）
_SCANNER = AccountScanner()


# ===================================================================
# ValidityChecker 类：单账号 / 全量账号的有效性检查
# ===================================================================
class ValidityChecker:
    """账号有效性检查器（负责浏览器验证 + 缓存 + 单个账号有效性翻转事件）。

    职责单一：
    - check_validity(p, a, extra)：单账号校验，超时 25s
    - check_all()：全账号并发 3 路校验 + 更新 last_check_all 缓存
    - check_one_and_record()：单账号校验 + 写缓存 + 可选进度回调 +
                              有效性翻转时（True<->False）向事件总线 publish
    """

    def __init__(self) -> None:
        # last_result / last_check_all 的读写锁
        self._cache_lock = threading.Lock()
        # 键 "{platform}|{account}"，值为 entry dict
        self._last_result: dict[str, dict] = {}
        # 最后一次 check_all() 的完整返回值列表 + 时间
        self._last_check_all: list[dict] | None = None
        self._last_check_all_time_ms: int = 0

    # ---------------------------------------------------------------
    # 旧体系检查（隔离 import/切换 EventLoopPolicy 副作用）
    # ---------------------------------------------------------------
    async def _check_legacy_account(self, legacy_type: int, legacy_file: str) -> bool:
        try:
            from myUtils.auth import check_cookie as legacy_check_cookie
        except Exception as e:
            logger.warning("_check_legacy: import myUtils.auth.check_cookie failed: %s", e)
            return False
        try:
            # myUtils.auth.check_cookie 内部同样会启动 Playwright 子进程，
            # SelectorEventLoop 不支持 create_subprocess_exec 会抛 NotImplementedError，
            # 所以必须临时切 ProactorPolicy
            with _proactor_policy_for_subprocess_local():
                return bool(await legacy_check_cookie(legacy_type, legacy_file))
        except Exception as e:
            logger.warning(
                "_check_legacy: check_cookie(type=%s, file=%s) error=%s: %s",
                legacy_type, legacy_file, type(e).__name__, e,
            )
            return False

    # ---------------------------------------------------------------
    # 对外主方法
    # ---------------------------------------------------------------
    async def check_validity(
        self,
        platform: str,
        account: str,
        legacy_type: int | None = None,
        legacy_file: str | None = None,
    ) -> bool:
        """单账号有效性检查；超时 25s 兜底。"""
        logger.debug(
            "check_validity: start platform=%s account=%s legacy_type=%s legacy_file=%s",
            platform, account, legacy_type, legacy_file,
        )
        start_ms = _now_ms()
        try:
            if legacy_type is not None and legacy_file is not None:
                is_valid = await asyncio.wait_for(
                    self._check_legacy_account(int(legacy_type), str(legacy_file)),
                    timeout=_SINGLE_CHECK_TIMEOUT_SEC,
                )
            else:
                fn = check_fns.get(platform)
                if fn is None:
                    logger.warning("check_validity: unknown platform=%s", platform)
                    return False
                is_valid = await asyncio.wait_for(
                    fn(account), timeout=_SINGLE_CHECK_TIMEOUT_SEC,
                )
            return bool(is_valid)
        except asyncio.TimeoutError:
            elapsed_ms = _now_ms() - start_ms
            logger.warning(
                "check_validity: TIMEOUT %ss platform=%s account=%s elapsed=%dms -> invalid",
                _SINGLE_CHECK_TIMEOUT_SEC, platform, account, elapsed_ms,
            )
            return False
        except Exception as e:
            elapsed_ms = _now_ms() - start_ms
            logger.warning(
                "check_validity: EXCEPTION platform=%s account=%s elapsed=%dms err=%s: %s",
                platform, account, elapsed_ms, type(e).__name__, e,
            )
            return False

    async def check_one_and_record(
        self,
        platform: str,
        account: str,
        sem: asyncio.Semaphore | None = None,
        progress_cb: Callable[[dict], None] | None = None,
        legacy_type: int | None = None,
        legacy_file: str | None = None,
    ) -> dict:
        """封装：单账号检查 + 更新缓存 + 可选进度回调 + 有效性翻转事件。

        为什么要单独封装而不是让 check_all/first_valid 自己写：
        - check_all / run_recheck_background / first_valid 三处都要"检查后写缓存"，
          重复代码容易不同步（比如漏了 TTL），抽一个方法统一做。
        """
        start_ms = _now_ms()
        if sem is not None:
            await sem.acquire()
        try:
            is_valid = await self.check_validity(
                platform, account, legacy_type=legacy_type, legacy_file=legacy_file,
            )
            elapsed_ms = _now_ms() - start_ms
            logger.info(
                "check_one_and_record: done platform=%s account=%s is_valid=%s elapsed=%dms",
                platform, account, is_valid, elapsed_ms,
            )
            entry = {
                "platform_key": platform,
                "account_name": account,
                "is_valid": is_valid,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "legacy_type": legacy_type,
                "legacy_file": legacy_file,
            }
            # 更新缓存，并检测有效性翻转 -> emit validity_switched 事件
            self._update_cache_and_emit_validity_switched(platform, account, entry)
            if progress_cb is not None:
                try:
                    progress_cb(entry)
                except Exception:
                    logger.exception("progress_cb 抛错，忽略")
            return entry
        finally:
            if sem is not None:
                sem.release()

    async def check_all(self, accounts: list[dict] | None = None) -> list[dict]:
        """全量账号有效性检查（并发 3 路 + 结果排序 + 写入全局 last_check_all 缓存）。"""
        if accounts is None:
            accounts = scan(emit_events=True)
        total = len(accounts)
        start_ms = _now_ms()
        logger.info("check_all: start total=%d", total)
        sem = asyncio.Semaphore(_CHECK_ALL_CONCURRENCY)
        tasks = [
            asyncio.create_task(self.check_one_and_record(
                acc["platform_key"], acc["account_name"],
                sem=sem,
                legacy_type=acc.get("legacy_type"),
                legacy_file=acc.get("legacy_file"),
            ))
            for acc in accounts
        ]
        results: list[dict] = []
        if tasks:
            done, _ = await asyncio.wait(tasks)
            for fut in done:
                try:
                    results.append(fut.result())
                except Exception:
                    # check_one_and_record 内部已吞大部分异常；这里兜 TaskCancel 等边角料
                    logger.exception("check_all 单任务异常 -> 按无效处理")
        results.sort(key=lambda r: (r["platform_key"], r["account_name"]))
        with self._cache_lock:
            self._last_check_all = results
            self._last_check_all_time_ms = _now_ms()
        valid_count = sum(1 for r in results if r.get("is_valid"))
        invalid_count = len(results) - valid_count
        logger.info(
            "check_all: done valid=%d invalid=%d total=%d elapsed=%dms",
            valid_count, invalid_count, total, _now_ms() - start_ms,
        )
        return results

    # ---------------------------------------------------------------
    # 缓存管理
    # ---------------------------------------------------------------
    def set_cache_entry(self, platform: str, account: str, entry: dict) -> None:
        """外部手工写入缓存（比如上传后顺手把该账号标记为有效）。"""
        self._update_cache_and_emit_validity_switched(platform, account, entry)

    def get_cached_entry(self, platform: str, account: str) -> dict | None:
        with self._cache_lock:
            return self._last_result.get(f"{platform}|{account}")

    def get_last_check_all(self) -> tuple[list[dict] | None, int]:
        """返回 (最后一次 check_all 结果, 完成的 timestamp_ms)。"""
        with self._cache_lock:
            return self._last_check_all, self._last_check_all_time_ms

    def _update_cache_and_emit_validity_switched(
        self, platform: str, account: str, entry: dict,
    ) -> None:
        """写细粒度缓存；有效性翻转时（True<->False）publish validity_switched 事件。

        为什么一定要发事件：
        - 上游关心"账号失效"是强业务事件（比如要停止再向该账号下发任务），
          只靠 30s heartbeat 的 accounts 快照轮询漏报窗口大。
        - 每次检查如果结果与上次缓存不同，必须立刻上送 account_sync。
        """
        key = f"{platform}|{account}"
        with self._cache_lock:
            prev = self._last_result.get(key)
            self._last_result[key] = entry
        prev_valid = None if prev is None else bool(prev.get("is_valid"))
        curr_valid = bool(entry.get("is_valid"))
        if prev is None:
            # 首次检查：不 emit "翻转"事件（扫描层 added 会处理首次上送）
            return
        if prev_valid != curr_valid:
            logger.info(
                "account validity switched platform=%s account=%s %s -> %s",
                platform, account, prev_valid, curr_valid,
            )
            _EVENT_BUS.publish(AccountEvent(
                source="checker",
                action="validity_switched",
                platform=platform,
                account=account,
                is_valid=curr_valid,
                checked_at=entry.get("checked_at"),
                legacy_type=entry.get("legacy_type"),
                legacy_file=entry.get("legacy_file"),
            ))


# 全局检查器（供顶层入口共享）
_CHECKER = ValidityChecker()


# ===================================================================
# RecheckTaskManager 类：后台账号检查任务进度追踪
# ===================================================================
class RecheckTaskManager:
    """管理后台 run_recheck_background 的进度追踪对象。

    对应 HTTP 接口：
    - POST /accounts/recheck -> next_recheck_task_id() + asyncio.create_task(run)
    - GET  /accounts/status?task_id=xxx -> get_current_recheck() 读取进度
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: RecheckTask | None = None
        self._id_counter = 0

    def next_id(self) -> str:
        with self._lock:
            self._id_counter += 1
            return f"recheck-{self._id_counter}"

    def register_running(self, task: RecheckTask) -> None:
        with self._lock:
            self._current = task

    def append_progress(self, entry: dict) -> None:
        with self._lock:
            if self._current is None:
                return
            self._current.accounts.append(entry)
            self._current.done += 1

    def mark_failed(self, error: str) -> None:
        with self._lock:
            if self._current is not None:
                self._current.status = "failed"
                self._current.error = error

    def mark_done(self) -> list[dict]:
        with self._lock:
            if self._current is None:
                return []
            self._current.status = "done"
            return list(self._current.accounts)

    def snapshot_current(self) -> RecheckTask | None:
        with self._lock:
            if self._current is None:
                return None
            return RecheckTask(
                task_id=self._current.task_id,
                started_at_ms=self._current.started_at_ms,
                status=self._current.status,
                total=self._current.total,
                done=self._current.done,
                accounts=list(self._current.accounts),
                error=self._current.error,
            )


_RECHECK_MANAGER = RecheckTaskManager()


# ===================================================================
# FirstValidResolver 类：单平台首个有效账号解析（缓存命中优先 + 负载可选）
# ===================================================================
class FirstValidResolver:
    """从给定平台的账号列表中，找到第一个有效账号（或按 round-robin 负载）。

    行为约束：
    - publish_task.account_name 已指定时：Dispatcher 直接跳过本类；本类只处理缺省场景
    - 单平台单账号（90% 场景）：直接返回该账号（命中缓存或首次浏览器验证）
    - 单平台多账号：按 round-robin_idx（文件名字典序递增）轮询使用，
      避免"永远打第一个账号，其他账号躺在磁盘吃灰"
    """

    def __init__(self) -> None:
        # 每个平台维护一个 round-robin 起点 index（线程安全）
        self._lock = threading.Lock()
        self._rr_idx: dict[str, int] = {}

    async def first_valid(self, platform: str) -> str | None:
        accounts = scan(emit_events=False)
        same_platform: list[dict] = [a for a in accounts if a["platform_key"] == platform]
        if not same_platform:
            logger.info("first_valid: platform=%s 没有任何账号文件", platform)
            return None
        # 多账号场景：按字典序排序（保证顺序稳定），然后按 rr 起点循环
        same_platform.sort(key=lambda r: r["account_name"])
        with self._lock:
            start = self._rr_idx.get(platform, 0) % max(1, len(same_platform))
            # 本类每次调用起点自增：保证下次从下一个账号开始（轮询）
            self._rr_idx[platform] = (start + 1) % len(same_platform)
        ordered = same_platform[start:] + same_platform[:start]

        for acc in ordered:
            account = acc["account_name"]
            logger.debug("first_valid: trying platform=%s account=%s rr_start=%s", platform, account, start)
            # 1) 命中缓存且 <10min TTL -> 直接返回，不再启动浏览器
            cached = _CHECKER.get_cached_entry(platform, account)
            if cached and cached.get("is_valid"):
                checked_at = cached.get("checked_at") or ""
                try:
                    dt = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
                    if (_now_ms() - int(dt.timestamp() * 1000)) < _CACHE_TTL_MS:
                        logger.info("first_valid: cache HIT platform=%s account=%s (rr_start=%s)",
                                    platform, account, start)
                        return account
                except Exception:
                    pass
            # 2) 缓存 miss 或过期 -> 启动浏览器真检查（带 TTL 更新缓存）
            if await _CHECKER.check_one_and_record(
                platform, account,
                legacy_type=acc.get("legacy_type"),
                legacy_file=acc.get("legacy_file"),
            ):
                entry_result = _CHECKER.get_cached_entry(platform, account)
                if entry_result and entry_result.get("is_valid"):
                    return account
        return None


_FIRST_VALID = FirstValidResolver()


# ===================================================================
# Cookie 目录轻量变更监听器（用轮询，避免引入 watchdog 依赖）
# ===================================================================
class CookieFilesPoller:
    """周期性扫描 cookies/ 与旧体系 cookiesFile/ 目录的"轮询监听器"。

    为什么不直接用 watchdog 第三方库：
    - 项目依赖里还没有 watchdog；多账号上报功能不需要这么重的依赖。
    - 账号操作（登录/登出/手动删）频率很低，3s 轮询完全能覆盖体验。
    - Session 0 服务进程看用户目录时 Windows ReadDirectoryChangesW 偶发丢事件，
      轮询在这种场景更可靠。
    """

    def __init__(self, interval_sec: float = 3.0) -> None:
        self._interval_sec = interval_sec
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """启动后台轮询线程（幂等：已启动则忽略）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="sau-account-poller",
            daemon=True,
        )
        self._thread.start()
        logger.info("CookieFilesPoller started: 每 %ss 扫描账号目录", self._interval_sec)

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        logger.info("CookieFilesPoller stopped")

    def _run_loop(self) -> None:
        # 为什么轮询时还要 try/except：
        # 扫描期间 cookies/ 目录被手动删除 / 权限异常 / 磁盘抖动都会抛
        # PermissionError/FileNotFoundError，监听器线程不能因此挂掉
        while not self._stop_evt.is_set():
            try:
                scan(emit_events=True)
            except Exception as e:
                logger.warning("CookieFilesPoller scan 异常（忽略）：%s: %s", type(e).__name__, e)
            self._stop_evt.wait(self._interval_sec)


# 全局轮询实例（core.py 初始化时 start；服务停止时 stop）
_COOKIE_POLLER = CookieFilesPoller()


def start_account_file_watcher() -> None:
    """顶层启动账号文件轮询监听器。"""
    _COOKIE_POLLER.start()


def stop_account_file_watcher() -> None:
    """顶层停止账号文件轮询监听器。"""
    _COOKIE_POLLER.stop()


# ===================================================================
# 账号删除（DELETE /accounts 对应实现）
# ===================================================================
def delete_account(platform: str, account: str) -> tuple[bool, str]:
    """删除指定账号（新体系优先，尝试删 cookies/{p}_{a}.json；
    如果是旧体系账号则删 cookiesFile/{uuid}.json + user_info 对应行）。

    Returns:
        (是否成功, 失败原因描述或 "ok")
    """
    # --- 1) 先尝试删新体系文件（最常见） ---
    # 为什么必须遍历目录按文件名匹配：account 名可能含字符被 JSON 文件名原样保留，
    # 直接 Path("{p}_{a}.json") 拼串与 scan 正则解出的结果一致，用 match 再次校验
    target_file: Path | None = None
    if _COOKIES_DIR.exists():
        for f in _COOKIES_DIR.iterdir():
            if not f.is_file():
                continue
            m = _COOKIE_PATTERN.match(f.name)
            if not m:
                continue
            if m.group(1) == platform and m.group(2) == account:
                target_file = f
                break
    deleted_new = False
    if target_file is not None:
        try:
            os.remove(str(target_file))
            logger.info("delete_account: remove new_style cookie file %s", target_file)
            deleted_new = True
        except Exception as e:
            return False, f"删除新体系 cookie 文件失败: {type(e).__name__}: {e}"

    # --- 2) 再尝试删旧体系 cookiesFile/{uuid}.json + user_info 行 ---
    deleted_legacy_file = False
    deleted_legacy_row = False
    legacy_uuid: str | None = None
    try:
        if _LEGACY_DB_PATH.exists():
            conn = sqlite3.connect(str(_LEGACY_DB_PATH))
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT filePath FROM user_info WHERE type=? AND userName=?",
                    (
                        next(k for k, v in _LEGACY_TYPE_TO_PLATFORM.items() if v == platform),
                        account,
                    ),
                )
                row = cur.fetchone()
                if row:
                    legacy_uuid = str(row[0])
                    cur.execute(
                        "DELETE FROM user_info WHERE type=? AND userName=?",
                        (
                            next(k for k, v in _LEGACY_TYPE_TO_PLATFORM.items() if v == platform),
                            account,
                        ),
                    )
                    conn.commit()
                    deleted_legacy_row = True
            finally:
                conn.close()
    except StopIteration:
        # 不在旧体系映射里（B站/百家号/YouTube）：忽略旧体系删除
        pass
    except Exception as e:
        logger.warning("delete_account: legacy db cleanup failed: %s", e)

    if legacy_uuid is not None and _LEGACY_COOKIES_DIR.exists():
        fpath = _LEGACY_COOKIES_DIR / str(legacy_uuid)
        try:
            if fpath.is_file():
                os.remove(str(fpath))
                deleted_legacy_file = True
        except Exception as e:
            logger.warning("delete_account: legacy cookie file remove failed: %s", e)

    if not (deleted_new or deleted_legacy_file or deleted_legacy_row):
        return False, f"账号不存在（找不到 {platform}/{account} 的 cookie 文件或数据库记录）"

    # 删除成功后：立即触发一次扫描（emit_events=True），让 Scanner diff 出 removed 事件，
    # 进而 SauAgentCore 收到 AccountEvent 立刻上送 account_sync
    scan(emit_events=True)
    return True, "ok"


# ===================================================================
# 顶层兼容函数（对外 API 保持不变，上游无需修改）
# ===================================================================

def scan(emit_events: bool = True) -> list[dict]:
    """[向后兼容入口] 扫描所有账号（新 + 旧）。"""
    return _SCANNER.scan(emit_events=emit_events)


async def check_validity(platform: str, account: str, **extra: Any) -> bool:
    """[向后兼容入口] 单账号有效性检查。"""
    return await _CHECKER.check_validity(
        platform, account,
        legacy_type=extra.get("legacy_type"),
        legacy_file=extra.get("legacy_file"),
    )


def set_last_check_cache(platform: str, account: str, entry: dict) -> None:
    """[向后兼容入口] 手工写入单账号缓存。"""
    _CHECKER.set_cache_entry(platform, account, entry)


def get_last_check_all() -> list[dict] | None:
    """[向后兼容入口] 返回最后一次 check_all 结果。"""
    res, _ = _CHECKER.get_last_check_all()
    return res


def get_current_recheck() -> RecheckTask | None:
    """[向后兼容入口] 取当前后台 recheck 任务快照。"""
    return _RECHECK_MANAGER.snapshot_current()


def next_recheck_task_id() -> str:
    """[向后兼容入口] 生成下一次 recheck 任务 id。"""
    return _RECHECK_MANAGER.next_id()


async def check_all() -> list[dict]:
    """[向后兼容入口] 全量账号有效性检查。"""
    return await _CHECKER.check_all()


async def run_recheck_background(task_id: str) -> None:
    """[向后兼容入口] 后台异步 check_all，进度写入 RecheckTask。"""
    start_ms = _now_ms()
    logger.info("run_recheck_background: start task_id=%s", task_id)
    accounts = scan(emit_events=True)
    task = RecheckTask(
        task_id=task_id,
        started_at_ms=_now_ms(),
        status="running",
        total=len(accounts),
        done=0,
        accounts=[],
    )
    _RECHECK_MANAGER.register_running(task)
    sem = asyncio.Semaphore(_CHECK_ALL_CONCURRENCY)

    def _on_progress(entry: dict) -> None:
        _RECHECK_MANAGER.append_progress(entry)

    try:
        tasks = [
            asyncio.create_task(_CHECKER.check_one_and_record(
                acc["platform_key"], acc["account_name"],
                sem=sem, progress_cb=_on_progress,
                legacy_type=acc.get("legacy_type"),
                legacy_file=acc.get("legacy_file"),
            ))
            for acc in accounts
        ]
        if tasks:
            done, pending = await asyncio.wait(tasks)
            for p in pending:
                p.cancel()
            for fut in done:
                try:
                    fut.result()
                except Exception:
                    logger.exception("run_recheck_background 单任务异常（按无效）")
    except Exception as exc:
        _RECHECK_MANAGER.mark_failed(f"{type(exc).__name__}: {exc}")
        logger.info("run_recheck_background: failed task_id=%s elapsed=%dms error=%s",
                    task_id, _now_ms() - start_ms, exc)
        return

    results = _RECHECK_MANAGER.mark_done()
    results.sort(key=lambda r: (r["platform_key"], r["account_name"]))
    # 回填全局 last_check_all（与 check_all 共享缓存）
    with _CHECKER._cache_lock:  # noqa: SLF001 - 同模块内部访问私有字段 OK
        _CHECKER._last_check_all = results  # noqa: SLF001
        _CHECKER._last_check_all_time_ms = _now_ms()  # noqa: SLF001
    logger.info("run_recheck_background: done task_id=%s elapsed=%dms total=%d",
                task_id, _now_ms() - start_ms, len(accounts))


async def first_valid(platform: str) -> str | None:
    """[向后兼容入口] 返回该平台第一个有效账号（多账号场景会 round-robin）。"""
    return await _FIRST_VALID.first_valid(platform)
