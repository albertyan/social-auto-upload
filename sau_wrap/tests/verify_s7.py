# -*- coding: utf-8 -*-
"""S7（半自动升级编排）验证脚本（任务 #18 第七步）。

场景：
1. 通知校验与版本比较（三字段/64 位小写 hex/语义化严格大于/强制 https/域名白名单）；
2. 后台下载（本地 http 假文件源）：成功落盘 + 校验通过；失败重试指数退避后成功；
   哈希不符删文件回 noticed（不重试）；
3. 单飞锁：并发 ensure_download 仅产生一次真实下载；
4. 状态机迁移全路径 + 原子持久化；
5. 编排六步（假执行器）：成功序列 / 失败自动回滚 → rolled_back / 回滚亦失败 → failed
   + 人工救援指引 / 前置条件不满足拒绝；
6. 启动自检三分支（§15.2：补校验置 success / 半替换回滚 / 无备份人工指引）；
7. 启动清理：>24h 备份与旧安装包删除、目标安装包保留；
8. 端点：/upgrade 快照、/upgrade/apply（未就绪 409、就绪触发编排、审计）、
   /upgrade/snooze（ready→snoozed）、Cookie 会话写操作 Nonce 防护、双鉴权共存。

说明：真实停服/安装器执行在开发环境不可全真验证，本脚本以可注入假执行器覆盖
编排全流程（真机验证留待 S8 打包步骤后）。

运行：``.venv\\Scripts\\python.exe sau_wrap\\tests\\verify_s7.py``
数据隔离：``SAU_DATA_ROOT`` → 本目录 ``_tmpdata7``。退出码 0=全部通过。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _REPO_ROOT)
os.environ["SAU_DATA_ROOT"] = os.path.join(_HERE, "_tmpdata7")

from aiohttp import web                                   # noqa: E402
from sau_wrap import paths                                # noqa: E402
from sau_wrap.agent import db as db_mod                   # noqa: E402
from sau_wrap.agent import ws_client as ws_mod            # noqa: E402
from sau_wrap.service.local_api import LocalApiServer     # noqa: E402
from sau_wrap.upgrade import orchestrator as orch_mod     # noqa: E402
from sau_wrap.upgrade import updater as updater_mod       # noqa: E402
from sau_wrap.upgrade.updater import (                    # noqa: E402
    Updater, validate_notice, version_gt, read_state_file,
)
from sau_wrap.upgrade.orchestrator import (               # noqa: E402
    Orchestrator, UpgradeExecutor, MANUAL_RESCUE_GUIDE,
)

RESULTS: list[str] = []
_TMP = Path(os.environ["SAU_DATA_ROOT"])
PAYLOAD = b"x" * (3 * 1024 * 1024 + 123)  # 跨 1MB 分块
PAYLOAD_HASH = hashlib.sha256(PAYLOAD).hexdigest()
#: 假下载源行为控制（每场景重置）
SRV = {"mode": "ok", "hits": 0, "payload": PAYLOAD, "hash": PAYLOAD_HASH}

updater_mod.DOWNLOAD_BACKOFF_BASE = 0.0  # 重试退避缩短为 0（测试提速）


def check(name: str, cond: bool, evidence: str) -> None:
    status = "PASS" if cond else "FAIL"
    RESULTS.append(f"[{status}] {name} :: {evidence}")
    print(f"[{status}] {name} :: {evidence}", flush=True)


def make_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    buf = io.StringIO()
    logger = logging.getLogger(f"sau.verify7.{name}.{time.time_ns()}")
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    return logger, buf


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_config(server_url: str = "wss://example.com/ws",
                 whitelist: list | None = None) -> None:
    paths.ensure_dir(paths.DATA_ROOT)
    obj = {"server_url": server_url, "agent_id": "ab" * 16,
           "update_domain_whitelist": whitelist or []}
    paths.CONFIG_FILE.write_text(json.dumps(obj), encoding="utf-8")


def fresh_env() -> None:
    for f in (paths.CONFIG_FILE, paths.CREDENTIAL_FILE, paths.LOCAL_TOKEN_FILE):
        try:
            os.remove(f)
        except OSError:
            pass
    shutil.rmtree(_TMP / "etc", ignore_errors=True)
    shutil.rmtree(_TMP / "updates", ignore_errors=True)
    db_mod.db_init()  # /status 依赖 local_tasks 表（每场景保证就绪）


def mk_updater(name: str, schemes=("http", "https"), **kw) -> Updater:
    logger, _ = make_logger(name)
    return Updater(logger,
                   state_file=_TMP / "etc" / f"upgrade_state-{name}.json",
                   updates_dir=_TMP / "updates" / name,
                   allowed_schemes=schemes, autoschedule=False, **kw)


def notice(url_host: str, port: int, version: str = "9.9.9",
           file_hash: str = PAYLOAD_HASH) -> dict:
    return {"version": version,
            "download_url": f"http://{url_host}:{port}/pkg/sau-{version}.exe",
            "file_hash": file_hash}


# ================================================================ 场景 1


def scenario_validate() -> None:
    print("\n==== 场景1：通知校验与版本语义化比较 ====", flush=True)
    check("版本比较：2.0.1>2.0.0 / 2.1.0>2.0.9 / 相等非大于",
          version_gt("2.0.1", "2.0.0") is True
          and version_gt("2.1.0", "2.0.9") is True
          and version_gt("2.0.0", "2.0.0") is False,
          "严格大于语义")
    check("版本比较：预发布低于正式版（2.0.0a0<2.0.0）、b1>a0",
          version_gt("2.0.0", "2.0.0a0") is True
          and version_gt("2.0.0a0", "2.0.0") is False
          and version_gt("2.0.0b1", "2.0.0a0") is True,
          "预发布后缀比较")
    check("版本比较：预发布数字段按数值比（终审修复14：a10>a9）",
          version_gt("2.0.0a10", "2.0.0a9") is True
          and version_gt("2.0.0a9", "2.0.0a10") is False,
          "2.0.0a10>2.0.0a9 严格大于")
    check("版本比较：非法版本返回 None",
          version_gt("abc", "1.0.0") is None
          and version_gt("1.0", "1.0.0") is None, "非语义化拒绝")

    cfg = SimpleNamespace(server_url="wss://example.com/ws",
                          update_domain_whitelist=[])
    good = {"version": "9.9.9",
            "download_url": "https://cdn.example.com/sau-9.9.9.exe",
            "file_hash": PAYLOAD_HASH}
    check("合法通知（回退 server_url host + 子域匹配）",
          validate_notice(good, "2.0.0a0", cfg)[0], "cdn.example.com 属 example.com 子域")

    bad1 = dict(good, version="")
    bad2 = dict(good, file_hash=PAYLOAD_HASH.upper())
    bad3 = dict(good, file_hash="abc123")
    check("三字段非空 + file_hash 64 位小写 hex",
          validate_notice(bad1, "2.0.0a0", cfg)[0] is False
          and validate_notice(bad2, "2.0.0a0", cfg)[0] is False
          and validate_notice(bad3, "2.0.0a0", cfg)[0] is False,
          "空字段/大写/短 hash 均拒绝")

    check("版本必须语义化且严格大于当前",
          validate_notice(dict(good, version="not-a-ver"), "2.0.0a0", cfg)[0] is False
          and validate_notice(dict(good, version="2.0.0a0"), "2.0.0a0", cfg)[0] is False
          and validate_notice(dict(good, version="1.0.0"), "2.0.0a0", cfg)[0] is False,
          "非法/相等/更低均拒绝")

    check("download_url 必须 https（默认仅允许 https）",
          validate_notice(dict(good, download_url="http://cdn.example.com/x.exe"),
                          "2.0.0a0", cfg)[0] is False,
          "http 被拒")

    check("域名白名单：白名单外拒绝、显式白名单通过",
          validate_notice(dict(good, download_url="https://evil.com/x.exe"),
                          "2.0.0a0", cfg)[0] is False
          and validate_notice(dict(good, download_url="https://evil.com/x.exe"),
                              "2.0.0a0",
                              SimpleNamespace(server_url="",
                                              update_domain_whitelist=["evil.com"]))[0]
          is True,
          "evil.com 默认拒 / 显式白名单放行")

    check("白名单与回退均为空 → 拒绝",
          validate_notice(good, "2.0.0a0",
                          SimpleNamespace(server_url="",
                                          update_domain_whitelist=[]))[0] is False,
          "无白名单来源")


# ================================================================ 场景 2/3


async def start_file_server() -> tuple[web.AppRunner, int]:
    async def handle(request: web.Request) -> web.Response:
        SRV["hits"] += 1
        if SRV["mode"] == "fail2" and SRV["hits"] <= 2:
            return web.Response(status=503, text="busy")
        return web.Response(body=SRV["payload"])

    app = web.Application()
    app.router.add_get("/pkg/{name}", handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, port


async def scenario_download() -> None:
    print("\n==== 场景2/3：后台下载（假文件源）+ 单飞锁 ====", flush=True)
    fresh_env()
    write_config(whitelist=["127.0.0.1"])
    runner, port = await start_file_server()
    try:
        # 2a. 正常下载（3MB 跨分块）→ ready + 落盘 + 校验通过
        SRV.update(mode="ok", hits=0, payload=PAYLOAD, hash=PAYLOAD_HASH)
        up = mk_updater("dl_ok")
        accepted = up.handle_notice(notice("127.0.0.1", port))
        await up.ensure_download()
        installer = up.updates_dir / "sau-9.9.9.exe"
        check("受理通知 → noticed；下载成功 → ready（SHA-256 校验通过、落盘）",
              accepted and up.state["phase"] == "ready" and installer.is_file()
              and up.state["verified"] is True
              and installer.read_bytes() == PAYLOAD,
              f"phase={up.state['phase']} size={installer.stat().st_size if installer.is_file() else '-'}")

        # 2b. 前 2 次 503，第 3 次成功（重试指数退避）
        SRV.update(mode="fail2", hits=0)
        up2 = mk_updater("dl_retry")
        up2.handle_notice(notice("127.0.0.1", port))
        await up2.ensure_download()
        check("下载失败重试（3 次尝试、指数退避）后成功",
              up2.state["phase"] == "ready" and SRV["hits"] == 3,
              f"hits={SRV['hits']} phase={up2.state['phase']}")

        # 2c. 哈希不符 → 删文件回 noticed，且不重试
        SRV.update(mode="ok", hits=0)
        up3 = mk_updater("dl_badhash")
        up3.handle_notice(notice("127.0.0.1", port, file_hash="0" * 64))
        await up3.ensure_download()
        leftovers = list(up3.updates_dir.glob("sau-9.9.9.exe*"))
        check("哈希不符 → 删文件回 noticed（不重试，等下次推送）",
              up3.state["phase"] == "noticed" and SRV["hits"] == 1
              and not leftovers and "SHA-256" in (up3.state["error"] or ""),
              f"hits={SRV['hits']} error={up3.state['error'][:40]}")

        # 3. 单飞锁：并发 ensure_download 仅一次真实下载
        SRV.update(mode="ok", hits=0)
        up4 = mk_updater("dl_single")
        up4.handle_notice(notice("127.0.0.1", port))
        await asyncio.gather(up4.ensure_download(), up4.ensure_download(),
                             up4.ensure_download())
        check("并发下载单飞锁：3 并发仅 1 次真实下载",
              SRV["hits"] == 1 and up4.state["phase"] == "ready",
              f"hits={SRV['hits']}")
    finally:
        await runner.cleanup()


# ================================================================ 场景 4


def scenario_state_machine() -> None:
    print("\n==== 场景4：状态机迁移全路径 + 原子持久化 ====", flush=True)
    fresh_env()
    up = mk_updater("sm")
    path_seq = ["noticed", "downloading", "ready", "snoozed",
                "applying", "success"]
    for p in path_seq:
        up.set_phase(p)
    on_disk = read_state_file(up.state_file)
    check("迁移全路径 noticed→…→success 且每步原子落盘",
          up.state["phase"] == "success"
          and on_disk is not None and on_disk["phase"] == "success"
          and on_disk.get("updated_at"),
          f"disk={on_disk['phase'] if on_disk else '-'}")
    up.set_phase("rolled_back")
    up.set_phase("failed", error="x")
    check("终态 rolled_back / failed 合法迁移",
          read_state_file(up.state_file)["phase"] == "failed",
          "八态全覆盖")


# ================================================================ 场景 5


@dataclass
class FakeExecutor:
    """假执行器：记录调用序列；可指定失败步骤与回滚失败步骤。"""
    calls: list = field(default_factory=list)
    fail_at: str = ""
    fail_also: tuple = ()
    verify_result: bool = True

    def _ok(self, name: str) -> bool:
        self.calls.append(name)
        return name != self.fail_at and name not in self.fail_also

    def stop_service(self) -> bool:
        return self._ok("stop_service")

    def backup_dir(self, src: Path, dst: Path) -> bool:
        return self._ok("backup_dir")

    def kill_tray(self) -> bool:
        return self._ok("kill_tray")

    def run_installer(self, p: Path) -> bool:
        return self._ok("run_installer")

    def start_service(self) -> bool:
        return self._ok("start_service")

    def restore_backup(self, src: Path, dst: Path) -> bool:
        return self._ok("restore_backup")

    def verify(self, target: str) -> bool:
        self.calls.append("verify")
        return self.verify_result

    def resolve_install_dir(self) -> Path:
        return _TMP / "fake_install"


def fake_exec_of(f: FakeExecutor) -> UpgradeExecutor:
    return UpgradeExecutor(
        stop_service=f.stop_service, backup_dir=f.backup_dir,
        kill_tray=f.kill_tray, run_installer=f.run_installer,
        start_service=f.start_service, restore_backup=f.restore_backup,
        verify=f.verify, resolve_install_dir=f.resolve_install_dir)


def ready_updater(tag: str) -> Updater:
    up = mk_updater(tag)
    paths.ensure_dir(up.updates_dir)
    installer = up.updates_dir / "sau-9.9.9.exe"
    installer.write_bytes(b"fake-installer")
    up.set_phase("ready", version="9.9.9", installer_path=str(installer),
                 verified=True)
    return up


def scenario_orchestration() -> None:
    print("\n==== 场景5：编排六步（假执行器）+ 回滚 ====", flush=True)
    fresh_env()
    logger, logbuf = make_logger("orch")

    up1 = ready_updater("o1")
    f1 = FakeExecutor()
    o1 = Orchestrator(logger, up1, fake_exec_of(f1))
    r1 = o1.apply()
    check("编排六步成功：调用序列完整 → success",
          r1["ok"] is True and r1["phase"] == "success"
          and f1.calls == ["stop_service", "backup_dir", "kill_tray",
                            "run_installer", "start_service", "verify"]
          and up1.state["phase"] == "success",
          f"calls={f1.calls}")

    up2 = ready_updater("o2")
    f2 = FakeExecutor(fail_at="run_installer")
    o2 = Orchestrator(logger, up2, fake_exec_of(f2))
    r2 = o2.apply()
    check("任一步失败 → 自动回滚（恢复备份+重启+二次校验）→ rolled_back",
          r2["phase"] == "rolled_back" and r2["failed_step"] == "[4/6] 静默安装"
          and up2.state["phase"] == "rolled_back"
          and "restore_backup" in f2.calls
          and f2.calls.index("run_installer") < f2.calls.index("restore_backup"),
          f"calls={f2.calls}")

    up3 = ready_updater("o3")
    f3 = FakeExecutor(fail_at="run_installer", fail_also=("restore_backup",))
    o3 = Orchestrator(logger, up3, fake_exec_of(f3))
    r3 = o3.apply()
    check("回滚亦失败 → failed + 人工救援指引",
          r3["phase"] == "failed"
          and up3.state["phase"] == "failed"
          and MANUAL_RESCUE_GUIDE in (up3.state["error"] or ""),
          f"error={(up3.state['error'] or '')[:60]}")

    up4 = mk_updater("o4")
    up4.set_phase("noticed", version="9.9.9")
    f4 = FakeExecutor()
    o4 = Orchestrator(logger, up4, fake_exec_of(f4))
    r4 = o4.apply()
    check("前置条件不满足（非 ready）→ 拒绝且不推进状态",
          r4["ok"] is False and up4.state["phase"] == "noticed"
          and f4.calls == [],
          f"resp={r4}")

    # 编排锁单飞（review 修正）：执行中再次 apply 立即拒绝
    up5 = ready_updater("o5")

    class _HoldExecutor(FakeExecutor):
        def stop_service(self) -> bool:
            self.calls.append("stop_service")
            hold.wait(5)  # 阻塞在第一步，等待并发 apply 发起
            return True

    f5 = _HoldExecutor()
    o5 = Orchestrator(logger, up5, fake_exec_of(f5))
    import threading as _th
    hold = _th.Event()
    done: dict = {}

    def _bg() -> None:
        done["r"] = o5.apply()

    t = _th.Thread(target=_bg, daemon=True)
    t.start()
    for _ in range(100):
        if f5.calls:
            break
        time.sleep(0.02)
    r_dup = o5.apply()
    hold.set()
    t.join(5)
    check("编排锁单飞：执行中再次 apply → 立即拒绝不并发",
          r_dup["ok"] is False and "已在进行中" in (r_dup.get("error") or "")
          and done.get("r", {}).get("phase") == "success"
          and f5.calls.count("stop_service") == 1,
          f"dup={r_dup.get('error')} final={done.get('r', {}).get('phase')}")


# ================================================================ 场景 6


def scenario_selfcheck() -> None:
    print("\n==== 场景6：启动自检三分支（§15.2）====", flush=True)
    fresh_env()
    logger, _ = make_logger("selfcheck")

    def applying_updater(tag: str, target: str = "9.9.9",
                         with_backup: bool = True) -> Updater:
        up = mk_updater(tag)
        up.set_phase("applying", version=target)
        if with_backup:
            paths.ensure_dir(up.backup_dir())
            (up.backup_dir() / "marker.txt").write_text("old")
        return up

    # 分支 1：当前版本==目标 → 补校验 → success → 清备份
    up1 = applying_updater("sc1")
    f1 = FakeExecutor(verify_result=True)
    r1 = Orchestrator(logger, up1, fake_exec_of(f1),
                      current_version="9.9.9").startup_selfcheck()
    check("分支1：版本达标 → 补做校验 → success，备份已清理",
          r1 == {"branch": 1, "phase": "success"}
          and up1.state["phase"] == "success"
          and not up1.backup_dir().exists() and "verify" in f1.calls,
          f"resp={r1}")

    up1b = applying_updater("sc1b")
    f1b = FakeExecutor(verify_result=False)
    r1b = Orchestrator(logger, up1b, fake_exec_of(f1b),
                       current_version="9.9.9").startup_selfcheck()
    check("分支1：版本达标但校验未通过 → 保持 applying 待下次自检",
          r1b["branch"] == 1 and up1b.state["phase"] == "applying",
          f"resp={r1b}")

    # 分支 2：版本不符且备份存在 → 自动回滚 → rolled_back
    up2 = applying_updater("sc2")
    f2 = FakeExecutor()
    r2 = Orchestrator(logger, up2, fake_exec_of(f2),
                      current_version="2.0.0a0").startup_selfcheck()
    check("分支2：半替换且备份存在 → 自动回滚 → rolled_back",
          r2["branch"] == 2 and up2.state["phase"] == "rolled_back"
          and "restore_backup" in f2.calls,
          f"resp={r2}")

    up2b = applying_updater("sc2b")
    f2b = FakeExecutor(fail_at="restore_backup")
    r2b = Orchestrator(logger, up2b, fake_exec_of(f2b),
                       current_version="2.0.0a0").startup_selfcheck()
    check("分支2：回滚失败 → failed + 人工救援指引",
          r2b["branch"] == 2 and up2b.state["phase"] == "failed"
          and MANUAL_RESCUE_GUIDE in (up2b.state["error"] or ""),
          f"resp={r2b}")

    # 分支 3：版本不符且无备份 → failed + 人工指引
    up3 = applying_updater("sc3", with_backup=False)
    f3 = FakeExecutor()
    r3 = Orchestrator(logger, up3, fake_exec_of(f3),
                      current_version="2.0.0a0").startup_selfcheck()
    check("分支3：无备份不可自愈 → failed + 人工救援指引",
          r3 == {"branch": 3, "phase": "failed"}
          and MANUAL_RESCUE_GUIDE in (up3.state["error"] or "")
          and f3.calls == [],
          f"resp={r3}")

    up4 = mk_updater("sc4")
    up4.set_phase("ready", version="9.9.9")
    r4 = Orchestrator(logger, up4, fake_exec_of(FakeExecutor()),
                      current_version="2.0.0a0").startup_selfcheck()
    check("非 applying 状态 → 自检不动作", r4 is None, "resp=None")


# ================================================================ 场景 7


def scenario_cleanup() -> None:
    print("\n==== 场景7：启动清理（>24h 备份与旧安装包）====", flush=True)
    fresh_env()
    up = mk_updater("clean")
    up.set_phase("ready", version="9.9.9")
    paths.ensure_dir(up.backup_dir())
    (up.backup_dir() / "old.txt").write_text("backup")
    old_installer = up.updates_dir / "sau-0.0.1.exe"
    old_installer.write_bytes(b"old")
    target_installer = up.updates_dir / "sau-9.9.9.exe"
    target_installer.write_bytes(b"target")
    old_ts = time.time() - 25 * 3600
    os.utime(up.backup_dir(), (old_ts, old_ts))
    os.utime(old_installer, (old_ts, old_ts))
    removed = up.cleanup_expired()
    check(">24h 备份与旧安装包被清理、目标安装包保留",
          not up.backup_dir().exists() and not old_installer.exists()
          and target_installer.exists() and len(removed) == 2,
          f"removed={[Path(p).name for p in removed]}")


# ================================================================ 场景 8


def session_cookie_of(resp) -> str:
    m = re.search(r"sau_session=([^;]+)", resp.headers.get("Set-Cookie", ""))
    return m.group(1) if m else ""


async def scenario_endpoints() -> None:
    print("\n==== 场景8：端点三态 + Nonce + 双鉴权 ====", flush=True)
    fresh_env()
    write_config(whitelist=["127.0.0.1"])
    logger, logbuf = make_logger("api")
    stop_event, resume_event = asyncio.Event(), asyncio.Event()
    client = ws_mod.WSClient(logger, stop_event, resume_event)
    up = mk_updater("api")
    api = LocalApiServer(logger, client, free_port(), updater=up)
    fake = FakeExecutor()
    api.attach_orchestrator(
        Orchestrator(logger, up, fake_exec_of(fake), local_api_port=api.port))
    await api.start()
    base = f"http://127.0.0.1:{api.port}"
    headers = {"X-SAU-Local-Token": api.token}
    try:
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            r0 = await sess.get(f"{base}/upgrade")
            check("GET /upgrade 未认证 → 401", r0.status == 401,
                  f"status={r0.status}")

            r1 = await sess.get(f"{base}/upgrade", headers=headers)
            b1 = await r1.json()
            check("GET /upgrade 只读快照（阶段/版本/进度/校验/错误字段）",
                  r1.status == 200 and "phase" in b1 and "progress" in b1
                  and "verified" in b1 and "current_version" in b1,
                  f"keys={sorted(b1)[:6]}")

            up.set_phase("noticed", version="9.9.9")
            r2 = await sess.post(f"{base}/upgrade/apply", headers=headers)
            check("POST /upgrade/apply 未就绪 → 409",
                  r2.status == 409
                  and (await r2.json())["error"] == "upgrade_not_ready",
                  f"status={r2.status}")
            rs = await sess.post(f"{base}/upgrade/snooze", headers=headers)
            check("POST /upgrade/snooze 非 ready → 409",
                  rs.status == 409, f"status={rs.status}")

            # ready → snooze → apply（snoozed 亦可确认）
            installer = up.updates_dir / "sau-9.9.9.exe"
            paths.ensure_dir(up.updates_dir)
            installer.write_bytes(b"fake")
            up.set_phase("ready", version="9.9.9",
                         installer_path=str(installer), verified=True)
            rz = await sess.post(f"{base}/upgrade/snooze", headers=headers)
            check("POST /upgrade/snooze：ready → snoozed",
                  rz.status == 200 and up.state["phase"] == "snoozed",
                  f"status={rz.status}")

            ra = await sess.post(f"{base}/upgrade/apply", headers=headers)
            b_ra = await ra.json()
            check("apply 单飞（review 修正）：端点同步置 applying + 响应 phase=applying",
                  ra.status == 200 and b_ra.get("phase") == "applying"
                  and up.state["phase"] in ("applying", "success"),
                  f"resp_phase={b_ra.get('phase')} state={up.state['phase']}")
            for _ in range(50):
                if up.state["phase"] in ("success", "failed", "rolled_back"):
                    break
                await asyncio.sleep(0.1)
            for _ in range(50):  # 审计由后台线程写，等待落盘后再断言
                if any("[AUDIT]" in ln and "op=upgrade_apply" in ln
                       for ln in logbuf.getvalue().splitlines()):
                    break
                await asyncio.sleep(0.1)
            audit_apply = [ln for ln in logbuf.getvalue().splitlines()
                           if "[AUDIT]" in ln and "op=upgrade_apply" in ln]
            check("POST /upgrade/apply（snoozed 确认）→ 编排执行 → success + 审计",
                  ra.status == 200 and up.state["phase"] == "success"
                  and any("via=token" in ln for ln in audit_apply),
                  f"status={ra.status} phase={up.state['phase']}")

            # Cookie 会话写操作：缺 Nonce 403 / 带 Nonce 成功（双鉴权 + §6.3）
            up.set_phase("ready", version="9.9.9",
                         installer_path=str(installer), verified=True)
            rt = await sess.post(f"{base}/ui-ticket", headers=headers)
            ticket = (await rt.json())["ticket"]
            rex = await sess.get(f"{base}/ui/t/{ticket}", allow_redirects=False)
            sid = session_cookie_of(rex)
            ck = {"sau_session": sid}
            rn = await sess.post(f"{base}/upgrade/apply", cookies=ck)
            check("Cookie 会话 /upgrade/apply 缺 Nonce → 403",
                  rn.status == 403
                  and (await rn.json())["error"] == "nonce_required",
                  f"status={rn.status}")
            nonce = (await (await sess.get(f"{base}/nonce",
                                           cookies=ck)).json())["nonce"]
            rc = await sess.post(f"{base}/upgrade/apply", cookies=ck,
                                 headers={"X-Console-Nonce": nonce})
            for _ in range(50):
                if up.state["phase"] in ("success", "failed", "rolled_back"):
                    break
                await asyncio.sleep(0.1)
            for _ in range(50):  # 同上：等后台线程审计落盘
                if any("via=cookie" in ln and "op=upgrade_apply" in ln
                       for ln in logbuf.getvalue().splitlines()):
                    break
                await asyncio.sleep(0.1)
            audit2 = [ln for ln in logbuf.getvalue().splitlines()
                      if "[AUDIT]" in ln and "op=upgrade_apply" in ln
                      and "via=cookie" in ln]
            check("Cookie 会话带 Nonce → 编排成功（双鉴权共存）+ 审计 via=cookie",
                  rc.status == 200 and up.state["phase"] == "success"
                  and bool(audit2),
                  f"status={rc.status} phase={up.state['phase']}")
    finally:
        await api.stop()


# ================================================================ 场景 9（终审修复①）


async def scenario_token_rotation() -> None:
    print("\n==== 场景9：升级校验令牌现读（终审修复①回归） ====", flush=True)
    fresh_env()
    logger, _ = make_logger("tok")
    stop_event, resume_event = asyncio.Event(), asyncio.Event()
    client = ws_mod.WSClient(logger, stop_event, resume_event)
    api = LocalApiServer(logger, client, free_port())
    await api.start()
    try:
        import urllib.request
        from sau_wrap.version import APP_VERSION

        old_token = api.token
        # 模拟服务重启令牌轮换（§3.6：每次启动重新生成并落盘）
        api._regenerate_token()  # noqa: SLF001（测试专用）
        new_token = paths.LOCAL_TOKEN_FILE.read_bytes().decode("utf-8").strip()
        check("令牌轮换：新令牌已落盘，read_local_token 现读即新值",
              new_token != old_token and orch_mod.read_local_token() == new_token,
              f"old[:8]={old_token[:8]} new[:8]={new_token[:8]}")

        # 反证：构造期旧快照令牌在轮换后必然 401（快照方案的死穴）
        req = urllib.request.Request(
            f"http://127.0.0.1:{api.port}/status",
            headers={"X-SAU-Local-Token": old_token})
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                old_ok = resp.status == 200
        except Exception:
            old_ok = False
        check("旧快照令牌请求 /status → 401（快照方案必败）", old_ok is False,
              "urlopen 抛 401 或非 200")

        # 修复语义：每次现读文件 → 轮换后校验仍通过（首轮命中，不等满窗口）
        # verify 为阻塞轮询（生产由编排线程调用），测试内经 to_thread 执行，
        # 避免阻塞事件循环导致 aiohttp 无法应答。
        ex = orch_mod.real_executors(logger, api.port,
                                     token_provider=orch_mod.read_local_token)
        ok = await asyncio.to_thread(ex.verify, APP_VERSION)
        check("令牌轮换后 verify（每次现读文件）仍通过", ok is True,
              f"target={APP_VERSION} verify={ok}")
    finally:
        await api.stop()


# ================================================================ 场景 10（终审修复⑤）


def scenario_restart_downloading() -> None:
    print("\n==== 场景10：downloading 重启重置（终审修复⑤回归） ====", flush=True)
    fresh_env()
    write_config(whitelist=["127.0.0.1"])
    name = "restart"
    state_file = _TMP / "etc" / f"upgrade_state-{name}.json"
    updates_dir = _TMP / "updates" / name
    paths.ensure_dir(state_file.parent)
    paths.ensure_dir(updates_dir)
    # 模拟上次服务在下载中崩溃：状态停留 downloading + 残留 .part 临时文件
    state_file.write_text(json.dumps({
        "phase": "downloading", "version": "9.9.9",
        "download_url": "https://127.0.0.1/pkg/sau-9.9.9.exe",
        "file_hash": PAYLOAD_HASH, "installer_path": None,
        "downloaded_bytes": 1234, "total_bytes": 99999,
        "verified": False, "error": None, "last_rejected": None,
        "updated_at": time.time()}), encoding="utf-8")
    part = updates_dir / "sau-9.9.9.exe.part"
    part.write_bytes(b"partial")

    up = mk_updater(name)
    check("重启时 downloading 状态自动重置为 noticed",
          up.state["phase"] == "noticed"
          and up.state.get("version") == "9.9.9"
          and "重置" in str(up.state.get("error") or ""),
          f"phase={up.state['phase']} version={up.state.get('version')}")
    check("未完成 .part 临时文件已清理", not part.exists(),
          f"part exists={part.exists()}")

    # 卡死根因反证修复：重置后可重新受理新通知（此前 downloading 态永被忽略）
    accepted = up.handle_notice(notice("127.0.0.1", 8080, version="9.9.9"))
    check("重置后 handle_notice 可再次受理（升级通道不再卡死）",
          accepted is True and up.state["phase"] == "noticed",
          f"accepted={accepted} phase={up.state['phase']}")


# ================================================================ 场景 11（终审修复⑧）


def scenario_runner_handoff() -> None:
    print("\n==== 场景11：runner 副本移交与执行（终审修复⑧回归） ====", flush=True)
    fresh_env()
    logger, _ = make_logger("runner")

    # ① 服务侧移交：执行器带 spawn_runner → 服务侧仅移交置 applying，不执行六步
    up1 = ready_updater("h1")
    spawned: list = []

    def ok_spawn(installer: Path, target: str) -> bool:
        spawned.append((str(installer), target))
        return True

    ex1 = fake_exec_of(FakeExecutor())
    ex1.spawn_runner = ok_spawn
    o1 = Orchestrator(logger, up1, ex1)
    r1 = o1.apply()
    check("服务侧移交：仅拉起 runner + 置 applying（六步不在服务进程执行）",
          r1 == {"ok": True, "phase": "applying", "handoff": "runner"}
          and spawned and spawned[0][1] == "9.9.9"
          and up1.state["phase"] == "applying",
          f"resp={r1} spawned={spawned}")

    # ② spawn_runner 失败 → failed + 救援指引（不进入六步）
    up2 = ready_updater("h2")
    ex2 = fake_exec_of(FakeExecutor())
    ex2.spawn_runner = lambda installer, target: False
    r2 = Orchestrator(logger, up2, ex2).apply()
    check("移交失败 → failed（升级中止，附人工救援指引）",
          r2["ok"] is False and r2["phase"] == "failed"
          and up2.state["phase"] == "failed"
          and "手动下载安装包" in str(up2.state.get("error") or ""),
          f"resp={r2}")

    # ③ runner 侧：run_upgrade 注入假执行器走全流程（含防递归移交验证）
    up3 = ready_updater("h3")
    up3.set_phase("applying")  # 服务侧移交后置位，runner 复用同一状态机继续
    f3 = FakeExecutor()
    ex3 = fake_exec_of(f3)
    spawn_calls: list = []
    ex3.spawn_runner = lambda installer, target: spawn_calls.append(1) or True
    from sau_wrap.upgrade import runner as runner_mod
    r3 = runner_mod.run_upgrade("ignored", "9.9.9",
                                executor=ex3, updater=up3, logger=logger)
    check("runner 侧全流程成功（防递归移交：spawn_runner 已置空未调）",
          r3["phase"] == "success" and up3.state["phase"] == "success"
          and f3.calls == ["stop_service", "backup_dir", "kill_tray",
                           "run_installer", "start_service", "verify"]
          and not spawn_calls,
          f"resp={r3} calls={f3.calls} spawn_calls={spawn_calls}")


# ================================================================ main


async def amain() -> int:
    scenario_validate()
    await scenario_download()
    scenario_state_machine()
    scenario_orchestration()
    scenario_selfcheck()
    scenario_cleanup()
    await scenario_endpoints()
    await scenario_token_rotation()
    scenario_restart_downloading()
    scenario_runner_handoff()
    failed = [r for r in RESULTS if r.startswith("[FAIL]")]
    print(f"\n==== 汇总：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ====",
          flush=True)
    return 1 if failed else 0


def main() -> int:
    code = asyncio.run(amain())
    report = os.path.join(_HERE, "_verify_report_s7.txt")
    with open(report, "w", encoding="utf-8") as f:
        f.write(f"S7 验证报告 @ {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("\n".join(RESULTS) + "\n")
    print(f"报告已写入: {report}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
