# -*- coding: utf-8 -*-
"""升级状态机与下载（实施计划 S7；设计文档 §7.4、现状文档 §7.3）。

状态机八态（持久化于 ``etc/upgrade_state.json``，原子写）：

    noticed / downloading / ready / snoozed / applying / success / failed / rolled_back

迁移（现状文档 §7.3 状态图）：

    [*] → noticed          收到合法 upgrade_notice 且版本严格大于当前
    noticed → downloading  开始下载（单飞锁防并发）
    downloading → ready    下载完成且 SHA-256 校验通过
    downloading → noticed  下载失败 / 哈希不符（删除文件，等下次推送或手动重播）
    ready ⇄ snoozed        用户暂缓 / 再次确认
    ready|snoozed → applying  控制台确认（POST /upgrade/apply，§7.4 唯一确认入口）
    applying → success / rolled_back / failed（编排结果，见 orchestrator）

通知校验（现状文档 §7.3 ``validate_notice``，防投毒）：
- 三字段（version / download_url / file_hash）非空；
- ``file_hash`` 必须 64 位小写 hex（SHA-256）；
- 版本语义化且**严格大于**当前 ``APP_VERSION``；
- ``download_url`` 必须 https（``allowed_schemes`` 构造参数仅供测试注入）；
- 域名 ∈ 白名单：``config.json`` 的 ``update_domain_whitelist``，缺省回退
  ``server_url`` 的 host，支持子域匹配。
不合法 → 记日志拒绝并记录 ``last_rejected``（供 ``GET /upgrade`` 排障可见）。

下载语义：``updates/sau-{version}.exe``，``.part`` 临时文件 + 1MB 分块边下边算
SHA-256 + 3 次尝试指数退避（2s→4s）+ 总超时 1800s；哈希不符删文件回 ``noticed``；
单飞锁防并发重复下载。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from sau_wrap import paths
from sau_wrap.version import APP_VERSION

#: 状态机八态（现状文档 §7.3）
PHASES = (
    "noticed", "downloading", "ready", "snoozed",
    "applying", "success", "failed", "rolled_back",
)

#: 状态持久化文件（现状文档 §7.3：SAU_HOME/etc/upgrade_state.json，原子写）
STATE_FILE: Path = paths.DATA_ROOT / "etc" / "upgrade_state.json"

#: 下载重试与节流参数（现状文档 §7.3：3 次尝试指数退避 2s 起；任务定义总超时 1800s）
DOWNLOAD_MAX_ATTEMPTS = 3
DOWNLOAD_BACKOFF_BASE = 2.0
DOWNLOAD_TOTAL_TIMEOUT = 1800.0
DOWNLOAD_CHUNK = 1024 * 1024  # 1MB 分块边下边算哈希

#: 安装包保留清理窗口（小时）：>24h 的备份与旧安装包启动时清理（§7.3 要点）
CLEANUP_AGE_HOURS = 24.0

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
#: 语义化版本：核心三段数字 + 可选预发布后缀（分隔符可省，如 2.0.0a0 / 2.1.0-beta1）
_SEMVER_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(?:[.-]?([0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*))?$")


def parse_semver(v: str):
    """解析语义化版本 → ``(core 三元组, pre 后缀|None)``；非法返回 None。"""
    m = _SEMVER_RE.match(str(v).strip())
    if not m:
        return None
    core = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return core, m.group(4)


def version_gt(a: str, b: str) -> bool | None:
    """``a > b`` 语义化严格比较；任一非法返回 None。

    规则：先比核心三段；核心相同则正式版大于任何预发布版，
    预发布之间按后缀字典序（2.0.0b1 > 2.0.0a0）。
    """
    pa, pb = parse_semver(a), parse_semver(b)
    if pa is None or pb is None:
        return None
    if pa[0] != pb[0]:
        return pa[0] > pb[0]
    pre_a, pre_b = pa[1], pb[1]
    if pre_a is None and pre_b is None:
        return False  # 相等
    if pre_a is None:
        return True   # 正式版 > 预发布版
    if pre_b is None:
        return False
    return pre_a > pre_b


def validate_notice(notice: dict, current_version: str = APP_VERSION,
                    cfg=None, allowed_schemes=("https",)) -> tuple[bool, str]:
    """``upgrade_notice`` 三字段校验（防投毒，现状文档 §7.3）。

    返回 ``(合法, 原因)``；``cfg`` 为 :class:`AgentConfig`（提供白名单与
    ``server_url`` 回退 host）。
    """
    version = str(notice.get("version") or "").strip()
    url = str(notice.get("download_url") or "").strip()
    file_hash = str(notice.get("file_hash") or "").strip()
    if not version or not url or not file_hash:
        return False, "字段缺失：version / download_url / file_hash 均不得为空"
    if not _HASH_RE.match(file_hash):
        return False, f"file_hash 必须为 64 位小写 hex（SHA-256），实际={file_hash[:16]}…"
    gt = version_gt(version, current_version)
    if gt is None:
        return False, f"version 非语义化版本：{version}"
    if not gt:
        return False, (f"version 未严格大于当前版本：notice={version} "
                       f"current={current_version}")
    parsed = urlparse(url)
    if parsed.scheme not in allowed_schemes:
        return False, f"download_url 必须为 {'/'.join(allowed_schemes)}：{url}"
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "download_url 缺少域名"
    domains = list(getattr(cfg, "update_domain_whitelist", None) or [])
    if not domains and getattr(cfg, "server_url", None):
        fallback = urlparse(cfg.server_url).hostname
        if fallback:
            domains = [fallback]
    if not domains:
        return False, "域名白名单为空（无 update_domain_whitelist 且未绑定无法回退）"
    for d in domains:
        d = str(d).strip().lower()
        if d and (host == d or host.endswith("." + d)):
            return True, ""
    return False, f"download_url 域名 {host} 不在白名单 {domains}"


def read_state_file(path: Path | None = None) -> dict | None:
    """读 ``upgrade_state.json``；不存在 / 非法 JSON / phase 非法 → None。"""
    f = path or STATE_FILE
    try:
        obj = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("phase") not in PHASES:
        return None
    return obj


def _empty_state() -> dict:
    return {"phase": None, "version": None, "download_url": None,
            "file_hash": None, "installer_path": None,
            "downloaded_bytes": 0, "total_bytes": 0,
            "verified": False, "error": None,
            "last_rejected": None, "updated_at": None}


class Updater:
    """升级状态机主体（通知接收校验 / 后台下载 / 持久化 / 端点快照）。

    参数：
    - ``state_file`` / ``updates_dir``：测试隔离注入（默认 ``paths`` 布局）；
    - ``allowed_schemes``：下载协议白名单，默认仅 ``https``（测试注入 ``http``）；
    - ``current_version``：默认 ``APP_VERSION``（§8.4 单一事实源）。

    编排（apply）见 :mod:`sau_wrap.upgrade.orchestrator`；本类只负责把状态推到
    ``ready`` 与持久化，``applying`` 后的推进由编排器完成。
    """

    def __init__(self, logger: logging.Logger,
                 state_file: Path | None = None,
                 updates_dir: Path | None = None,
                 allowed_schemes: tuple[str, ...] = ("https",),
                 current_version: str = APP_VERSION,
                 autoschedule: bool = True) -> None:
        self._logger = logger
        self.state_file = Path(state_file or STATE_FILE)
        self.updates_dir = Path(updates_dir or paths.UPDATES_DIR)
        self.allowed_schemes = tuple(allowed_schemes)
        self.current_version = current_version
        #: 受理通知后是否自动调度后台下载（测试可关，显式 await ensure_download）
        self._autoschedule = autoschedule
        self._lock = asyncio.Lock()          # 下载单飞锁（§7.3 要点）
        #: 共享状态互斥锁（review 修正）：``state`` 字典由编排后台线程写、
        #: 事件循环（下载进度/端点快照）读写，跨线程访问统一经此锁。
        self._state_lock = threading.Lock()
        self._notice: dict | None = None     # 已受理通知（下载中供快照展示）
        self.state: dict = read_state_file(self.state_file) or _empty_state()

    # ------------------------------------------------------------ 持久化

    def _persist(self) -> None:
        """原子写状态文件（现状文档 §7.3：临时文件 + os.replace）。"""
        self.state["updated_at"] = time.time()
        paths.ensure_dir(self.state_file.parent)
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp.write_bytes(json.dumps(self.state, ensure_ascii=False,
                                   indent=2).encode("utf-8"))
        os.replace(tmp, self.state_file)

    def set_phase(self, phase: str, **fields) -> None:
        """状态迁移 + 落盘（供编排器与自检复用；跨线程经 ``_state_lock``）。"""
        assert phase in PHASES, f"非法 phase：{phase}"
        with self._state_lock:
            self.state["phase"] = phase
            self.state.update(fields)
            self._persist()
        self._logger.info("升级状态机迁移 → %s（version=%s）",
                          phase, self.state.get("version"))

    # ------------------------------------------------------------ 通知接收

    def handle_notice(self, notice: dict) -> bool:
        """处理 ``upgrade_notice``（ws_client 接线）。合法 → noticed 并调度下载。

        返回是否受理；不合法记日志拒绝并落 ``last_rejected``（排障可见）。
        """
        from sau_wrap.agent import config as agent_config

        ok, reason = validate_notice(
            notice, self.current_version, agent_config.load_config(),
            allowed_schemes=self.allowed_schemes)
        if not ok:
            self._logger.warning("upgrade_notice 校验失败，已拒绝：%s（notice=%s）",
                                 reason, notice)
            with self._state_lock:
                self.state["last_rejected"] = {
                    "version": str(notice.get("version") or ""),
                    "reason": reason, "at": time.time(),
                }
                self._persist()
            return False

        version = str(notice["version"]).strip()
        phase = self.state.get("phase")
        if phase == "downloading":
            self._logger.info("升级下载中，忽略重复 upgrade_notice（version=%s）", version)
            return False
        if phase == "applying":
            self._logger.info("升级编排中，忽略 upgrade_notice（version=%s）", version)
            return False
        if phase in ("ready", "snoozed") and self.state.get("version") == version:
            self._logger.info("升级已就绪/暂缓（version=%s），忽略重复通知", version)
            return False
        if phase == "success" and version == self.current_version:
            return False  # 已升完目标版本

        self._notice = dict(notice)
        self.set_phase("noticed", version=version,
                       download_url=str(notice["download_url"]).strip(),
                       file_hash=str(notice["file_hash"]).strip(),
                       installer_path=None, downloaded_bytes=0, total_bytes=0,
                       verified=False, error=None)
        # 服务进程内有运行中的事件循环 → 自动调度后台下载；
        # 测试可关 autoschedule 显式 await ensure_download()。
        if not self._autoschedule:
            return True
        try:
            asyncio.get_running_loop().create_task(self._download_task())
        except RuntimeError:
            self._logger.info("无运行中的事件循环：下载待显式 ensure_download() 触发")
        return True

    # ------------------------------------------------------------ 后台下载

    async def ensure_download(self) -> None:
        """单飞下载入口：并发调用仅首个真正下载，其余等锁后按状态跳过。"""
        async with self._lock:
            if self.state.get("phase") == "ready":
                return
            notice = self._notice
            if notice is None and self.state.get("phase") == "noticed":
                notice = {"version": self.state.get("version"),
                          "download_url": self.state.get("download_url"),
                          "file_hash": self.state.get("file_hash")}
            if notice is None:
                return
            self.set_phase("downloading", downloaded_bytes=0,
                           total_bytes=0, error=None)
            try:
                installer = await self._download_once(
                    str(notice["download_url"]), str(notice["file_hash"]),
                    str(notice["version"]))
            except Exception as exc:  # 下载失败 / 哈希不符 → 回 noticed（§7.3）
                self._logger.error("升级安装包下载失败：%s（回 noticed，等下次推送/重播）",
                                   exc)
                self.set_phase("noticed", error=str(exc),
                               downloaded_bytes=0, total_bytes=0)
                return
            self.set_phase("ready", installer_path=str(installer),
                           verified=True, error=None)

    async def _download_task(self) -> None:
        try:
            await self.ensure_download()
        except Exception:  # pragma: no cover - 兜底：任务级异常不落状态机之外
            self._logger.exception("升级下载任务异常")

    async def _download_once(self, url: str, file_hash: str,
                             version: str) -> Path:
        """下载 + 边下边算 SHA-256（3 次尝试、指数退避 2s→4s、总超时 1800s）。

        哈希不符**不重试**（重试必然同果）：删文件抛错，由上层回 ``noticed``。
        """
        paths.ensure_dir(self.updates_dir)
        dest = self.updates_dir / f"sau-{version}.exe"
        part = dest.with_suffix(dest.suffix + ".part")
        deadline = time.monotonic() + DOWNLOAD_TOTAL_TIMEOUT
        timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TOTAL_TIMEOUT)
        last_err = ""
        for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"下载总超时 {DOWNLOAD_TOTAL_TIMEOUT:.0f}s")
            if part.exists():
                part.unlink()
            try:
                hasher = hashlib.sha256()
                downloaded = 0
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    async with sess.get(url) as resp:
                        if resp.status != 200:
                            raise IOError(f"HTTP {resp.status}")
                        total = int(resp.headers.get("Content-Length") or 0)
                        with self._state_lock:
                            self.state["total_bytes"] = total
                        with open(part, "wb") as f:
                            async for chunk in resp.content.iter_chunked(
                                    DOWNLOAD_CHUNK):
                                f.write(chunk)
                                hasher.update(chunk)
                                downloaded += len(chunk)
                                with self._state_lock:
                                    self.state["downloaded_bytes"] = downloaded
                digest = hasher.hexdigest()
                if digest != file_hash.lower():
                    raise ValueError(
                        f"SHA-256 不匹配：期望 {file_hash[:16]}… 实际 {digest[:16]}…")
                os.replace(part, dest)
                self._logger.info("安装包下载完成并校验通过：%s（%d 字节）",
                                  dest, downloaded)
                return dest
            except ValueError:
                # 哈希不符：删除文件，不重试（现状文档 §7.3）
                if part.exists():
                    part.unlink()
                raise
            except Exception as exc:
                last_err = f"第 {attempt}/{DOWNLOAD_MAX_ATTEMPTS} 次下载失败：{exc}"
                self._logger.warning(last_err)
                if part.exists():
                    part.unlink()
                if attempt < DOWNLOAD_MAX_ATTEMPTS:
                    await asyncio.sleep(DOWNLOAD_BACKOFF_BASE * (2 ** (attempt - 1)))
        raise IOError(last_err or "下载失败")

    # ------------------------------------------------------------ 端点支撑

    def snapshot(self) -> dict:
        """``GET /upgrade`` 只读快照（§7.4：阶段/目标版本/下载进度/校验/错误）。"""
        with self._state_lock:
            st = dict(self.state)
        total = int(st.get("total_bytes") or 0)
        done = int(st.get("downloaded_bytes") or 0)
        return {
            "phase": st.get("phase"),
            "version": st.get("version"),
            "current_version": self.current_version,
            "download_url": st.get("download_url"),
            "installer_path": st.get("installer_path"),
            "progress": {
                "downloaded_bytes": done,
                "total_bytes": total,
                "percent": round(done * 100.0 / total, 1) if total else None,
            },
            "verified": bool(st.get("verified")),
            "error": st.get("error"),
            "last_rejected": st.get("last_rejected"),
            "updated_at": st.get("updated_at"),
        }

    def installer_path(self) -> Path | None:
        p = self.state.get("installer_path")
        return Path(p) if p else None

    def backup_dir(self) -> Path:
        """安装目录备份旁路（编排/回滚共用，§7.4 步骤 2）。"""
        return self.updates_dir / "backup"

    # ------------------------------------------------------------ 启动清理

    def cleanup_expired(self) -> list[str]:
        """启动时清理 >24h 的备份与旧安装包（§7.3 要点；任务定义 7）。"""
        removed: list[str] = []
        cutoff = time.time() - CLEANUP_AGE_HOURS * 3600
        backup = self.backup_dir()
        if backup.exists():
            try:
                if backup.stat().st_mtime < cutoff:
                    import shutil
                    shutil.rmtree(backup, ignore_errors=True)
                    removed.append(str(backup))
                    self._logger.info("已清理过期升级备份（>24h）：%s", backup)
            except OSError:
                pass
        if self.updates_dir.exists():
            target = self.state.get("version")
            for f in self.updates_dir.glob("sau-*.exe*"):
                try:
                    if f.stat().st_mtime < cutoff or (
                            target and f.name != f"sau-{target}.exe"):
                        f.unlink()
                        removed.append(str(f))
                        self._logger.info("已清理过期/旧版安装包：%s", f)
                except OSError:
                    pass
        return removed
