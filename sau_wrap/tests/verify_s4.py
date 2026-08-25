# -*- coding: utf-8 -*-
"""S4（任务执行核心 dispatcher）场景验证脚本（任务 #15 第四步）。

场景：
1. 成功链路：publish_task → 落库 queued → running → mock 上传成功 →
   task_result success 回达 + downloads 清理 + account_sync 上行；
2. 素材 403 → 上行 file_renew → 服务端回 file_renewed（新 URL）→ 重试成功；
3. cookie 错误分类 → failed 且不自动重试（attempts=1）；
4. 并发信号量：发 3 任务，同时运行 ≤2（max_concurrency=2）；
5. 重启恢复：DB 预置 queued/running 任务 → 直接调 recover_pending() 重新入队执行。

运行（仓库根目录）：
    .venv\\Scripts\\python.exe sau_wrap\\tests\\verify_s4.py

数据隔离：SAU_DATA_ROOT 指向本目录下 _tmpdata4（不触碰 %ProgramData%\\SAU）。
上传全部走 ``upstream_adapter.register_uploader`` 注入的假函数（不拉起上游
playwright）；下载走注入的假下载器（场景 2 首包抛 FileRenewNeeded）。
退出码：0=全部通过；1=存在失败。报告写 ``_verify_report_s4.txt``（UTF-8）。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import shutil
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _REPO_ROOT)
os.environ["SAU_DATA_ROOT"] = os.path.join(_HERE, "_tmpdata4")

from sau_wrap import paths                                    # noqa: E402
from sau_wrap.agent import config as agent_config             # noqa: E402
from sau_wrap.agent import db                                 # noqa: E402
from sau_wrap.agent import upstream_adapter                   # noqa: E402
from sau_wrap.agent import ws_client as ws_mod                # noqa: E402
from sau_wrap.agent.dispatcher import (                       # noqa: E402
    FileRenewNeeded,
    TaskDispatcher,
)
from sau_wrap.tests.mock_ws_server import MockAgentServer     # noqa: E402

RESULTS: list[str] = []
FAKE_MACHINE = "ab" * 16


def check(name: str, cond: bool, evidence: str) -> None:
    status = "PASS" if cond else "FAIL"
    RESULTS.append(f"[{status}] {name} :: {evidence}")
    print(f"[{status}] {name} :: {evidence}", flush=True)


def make_logger() -> tuple[logging.Logger, io.StringIO]:
    buf = io.StringIO()
    logger = logging.getLogger(f"sau.verify4.{time.time_ns()}")
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    return logger, buf


def fresh_env() -> None:
    """清理 config/凭证/DB/下载目录，重建干净环境（含测试 cookie 文件）。"""
    for f in (paths.CONFIG_FILE, paths.CREDENTIAL_FILE, paths.DB_FILE):
        with contextlib.suppress(OSError):
            os.remove(f)
    for d in (paths.DOWNLOADS_DIR, paths.COOKIES_DIR):
        shutil.rmtree(d, ignore_errors=True)
    db.db_init()
    with db._LOCK, db._connect() as conn:  # noqa: SLF001（测试专用）
        conn.execute("DELETE FROM local_tasks")
        conn.execute("DELETE FROM result_queue")
    # 测试账号 cookie（合法 JSON → is_valid=True；mock publish 的 account_name）
    paths.ensure_dir(paths.COOKIES_DIR)
    (paths.COOKIES_DIR / "douyin_mock-account.json").write_text(
        json.dumps({"cookies": []}), encoding="utf-8")


def bind_to(url: str) -> None:
    agent_config.bind(url, "mock_token_" + "x" * 16)


async def wait_until(pred, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return pred()


async def stop_client(client_task: asyncio.Task, stop_event: asyncio.Event) -> None:
    stop_event.set()
    with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(client_task, timeout=10)


def _db_row(sql: str, args: tuple = ()):
    with db._LOCK, db._connect() as conn:
        return conn.execute(sql, args).fetchone()


def _task_status(task_id: str) -> str | None:
    row = _db_row("SELECT status FROM local_tasks WHERE task_id=?", (task_id,))
    return row[0] if row else None


def _task_attempts(task_id: str) -> int:
    row = _db_row("SELECT attempts FROM local_tasks WHERE task_id=?", (task_id,))
    return int(row[0]) if row else -1


def _make_client_stack(logger, stop_event, resume_event, *, downloader=None,
                       after_task_hook=None):
    """组装 WSClient + TaskDispatcher（与 host.py 同款接线）。"""
    client = ws_mod.WSClient(logger, stop_event, resume_event)
    cfg = agent_config.load_config()
    dispatcher = TaskDispatcher(
        logger,
        result_sender=client.submit_task_result,
        file_renew_sender=client.send_file_renew,
        scheduling_paused=lambda: False,
        stop_event=stop_event,
        max_concurrency=cfg.max_concurrency if cfg else 2,
        downloader=downloader,
        after_task_hook=after_task_hook or client.send_account_sync,
    )
    client.attach_dispatcher(dispatcher)
    return client, dispatcher


async def _fake_downloader(url: str, dest) -> None:
    """默认假下载器：直接写一个占位文件（模拟下载成功）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"fake-media")


# ================================================================ 场景 1


async def scenario_success_chain() -> None:
    print("\n==== 场景1：成功链路（落库 → 执行 → task_result success）====", flush=True)
    fresh_env()
    server = MockAgentServer()
    await server.start()
    bind_to(server.url)
    server.on_connect_actions.append("publish:T-001")

    calls: list[dict] = []

    async def fake_upload(*, payload, account_file, video_file, image_files):
        calls.append({"payload": payload, "account_file": account_file,
                      "video_file": str(video_file),
                      "video_exists": video_file.is_file() if video_file else False})
        return ""

    upstream_adapter.register_uploader("douyin", "video", fake_upload)
    logger, logbuf = make_logger()
    stop_event, resume_event = asyncio.Event(), asyncio.Event()
    client, dispatcher = _make_client_stack(
        logger, stop_event, resume_event, downloader=_fake_downloader)
    task = asyncio.create_task(client.run())
    try:
        ok = await wait_until(
            lambda: len(server.received_of("task_result")) >= 1, timeout=15)
        results = server.received_of("task_result")
        r = results[0]["data"] if results else {}
        check("publish_task → 上传成功 → task_result success 回达",
              ok and r.get("task_id") == "T-001" and r.get("status") == "success",
              f"task_result={json.dumps(r, ensure_ascii=False)[:160]}")
        check("落库终态 success 且 attempts=1",
              _task_status("T-001") == "success" and _task_attempts("T-001") == 1,
              f"status={_task_status('T-001')} attempts={_task_attempts('T-001')}")
        check("假上传函数被调用且素材文件真实落盘",
              len(calls) == 1 and calls[0]["video_exists"]
              and calls[0]["account_file"].endswith("douyin_mock-account.json"),
              f"calls={len(calls)} video_exists={calls[0]['video_exists'] if calls else '-'} "
              f"account={os.path.basename(calls[0]['account_file']) if calls else '-'}")
        check("downloads/{task_id} 完成后已清理",
              not (paths.DOWNLOADS_DIR / "T-001").exists(),
              f"目录存在={(paths.DOWNLOADS_DIR / 'T-001').exists()}")

        # —— manual 语义：非 tencent 平台 submit_mode=manual → 直接发布 + 备注
        await server.send_to_client("publish_task", {
            "task_id": "T-002", "platform_key": "douyin", "content_type": "video",
            "file_url": "https://example.invalid/v2.mp4", "media_urls": [],
            "title": "t2", "description": "", "tags": [],
            "account_name": "mock-account", "submit_mode": "manual",
            "scheduled_at": int(time.time() * 1000),
        })
        ok2 = await wait_until(
            lambda: any(m["data"]["task_id"] == "T-002"
                        for m in server.received_of("task_result")), timeout=10)
        r2 = next((m["data"] for m in server.received_of("task_result")
                   if m["data"]["task_id"] == "T-002"), {})
        check("manual 非草稿平台：直接发布且备注说明",
              ok2 and r2.get("status") == "success" and "草稿" in (r2.get("remarks") or ""),
              f"remarks={r2.get('remarks')!r}")
        # —— account_sync：任务结束后触发（注册首包亦带账号快照）
        syncs = server.received_of("account_sync")
        regs = server.received_of("register")
        reg_accounts = regs[0]["data"]["accounts"] if regs else None
        check("account_sync 上行（任务完成后触发）+ 注册首包带账号快照",
              len(syncs) >= 2 and reg_accounts is not None
              and any(a["account_name"] == "mock-account"
                      for a in (syncs[-1]["data"]["accounts"] if syncs else [])),
              f"account_sync 次数={len(syncs)} register.accounts={reg_accounts}")
        # —— 重推保护：已 success 的任务服务端补推（结果积压未回达的现实路径）→ 跳过不重复执行
        calls_before = len(calls)
        await server.send_to_client("publish_task", {
            "task_id": "T-001", "platform_key": "douyin", "content_type": "video",
            "file_url": "https://example.invalid/mock.mp4", "media_urls": [],
            "title": "mock 任务标题", "description": "mock 描述", "tags": ["mock"],
            "account_name": "mock-account", "submit_mode": "auto",
            "scheduled_at": int(time.time() * 1000),
        })
        await asyncio.sleep(2)  # 观察窗：不得再次执行
        check("成功后重推不再执行（防重复发布）",
              len(calls) == calls_before and _task_status("T-001") == "success",
              f"upload 调用 {calls_before}→{len(calls)} status={_task_status('T-001')}"
              f" 日志含跳过={'跳过重推' in logbuf.getvalue()}")
    finally:
        upstream_adapter.unregister_uploader("douyin", "video")
        await stop_client(task, stop_event)
        await server.stop()


# ================================================================ 场景 2


async def scenario_file_renew() -> None:
    print("\n==== 场景2：403 → file_renew → file_renewed → 重试成功 ====", flush=True)
    fresh_env()
    server = MockAgentServer()
    await server.start()
    bind_to(server.url)
    server.on_connect_actions.append("publish:T-100")

    dl_calls: list[str] = []

    async def flaky_downloader(url: str, dest) -> None:
        dl_calls.append(url)
        if len(dl_calls) == 1:
            raise FileRenewNeeded(f"403 签名过期: {url}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-media-renewed")

    async def fake_upload(*, payload, account_file, video_file, image_files):
        return ""

    upstream_adapter.register_uploader("douyin", "video", fake_upload)
    logger, logbuf = make_logger()
    stop_event, resume_event = asyncio.Event(), asyncio.Event()
    client, dispatcher = _make_client_stack(
        logger, stop_event, resume_event, downloader=flaky_downloader)
    task = asyncio.create_task(client.run())
    try:
        # 等客户端上行 file_renew 后，服务端回 file_renewed（换新 URL）
        ok_renew = await wait_until(
            lambda: len(server.received_of("file_renew")) >= 1, timeout=10)
        if ok_renew:
            await server.send_to_client("file_renewed", {
                "task_id": "T-100",
                "file_url": "https://example.invalid/renewed.mp4",
            })
        ok = await wait_until(
            lambda: any(m["data"]["task_id"] == "T-100" and m["data"]["status"] == "success"
                        for m in server.received_of("task_result")), timeout=15)
        check("file_renew 上行（403 触发）", ok_renew
              and server.received_of("file_renew")[0]["data"]["task_id"] == "T-100",
              f"file_renew={[m['data'] for m in server.received_of('file_renew')]}")
        check("file_renewed 后以新 URL 重试成功", ok and len(dl_calls) == 2
              and dl_calls[1] == "https://example.invalid/renewed.mp4",
              f"下载调用序列={dl_calls}")
        check("新 URL 回写 local_tasks.payload（重启恢复可续用）",
              "renewed.mp4" in (_db_row(
                  "SELECT payload FROM local_tasks WHERE task_id='T-100'") or [""])[0],
              f"status={_task_status('T-100')}")
    finally:
        upstream_adapter.unregister_uploader("douyin", "video")
        await stop_client(task, stop_event)
        await server.stop()


# ================================================================ 场景 3


async def scenario_cookie_error() -> None:
    print("\n==== 场景3：cookie 错误 → failed 且不自动重试 ====", flush=True)
    fresh_env()
    server = MockAgentServer()
    await server.start()
    bind_to(server.url)
    server.on_connect_actions.append("publish:T-001")

    calls = {"n": 0}

    async def cookie_fail_upload(*, payload, account_file, video_file, image_files):
        calls["n"] += 1
        raise RuntimeError("browser cookie expired, please login again")

    upstream_adapter.register_uploader("douyin", "video", cookie_fail_upload)
    logger, logbuf = make_logger()
    stop_event, resume_event = asyncio.Event(), asyncio.Event()
    client, dispatcher = _make_client_stack(
        logger, stop_event, resume_event, downloader=_fake_downloader)
    task = asyncio.create_task(client.run())
    try:
        ok = await wait_until(
            lambda: any(m["data"]["task_id"] == "T-001" and m["data"]["status"] == "failed"
                        for m in server.received_of("task_result")), timeout=15)
        r = next((m["data"] for m in server.received_of("task_result")
                  if m["data"]["task_id"] == "T-001"), {})
        await asyncio.sleep(3)  # 重试观察窗：不得有第二次上传调用
        check("cookie 错误分类 → failed 且 error 含分类说明",
              ok and "cookie" in (r.get("error") or "").lower(),
              f"error={r.get('error')!r}")
        check("不自动重试（上传仅调用 1 次、attempts=1）",
              calls["n"] == 1 and _task_attempts("T-001") == 1,
              f"upload_calls={calls['n']} attempts={_task_attempts('T-001')}")
    finally:
        upstream_adapter.unregister_uploader("douyin", "video")
        await stop_client(task, stop_event)
        await server.stop()


# ================================================================ 场景 4


async def scenario_concurrency() -> None:
    print("\n==== 场景4：并发信号量（3 任务，同时运行 ≤2）====", flush=True)
    fresh_env()
    server = MockAgentServer()
    await server.start()
    bind_to(server.url)

    state = {"running": 0, "max": 0}

    async def slow_upload(*, payload, account_file, video_file, image_files):
        state["running"] += 1
        state["max"] = max(state["max"], state["running"])
        try:
            await asyncio.sleep(1.5)
        finally:
            state["running"] -= 1
        return ""

    upstream_adapter.register_uploader("douyin", "video", slow_upload)
    logger, logbuf = make_logger()
    stop_event, resume_event = asyncio.Event(), asyncio.Event()
    client, dispatcher = _make_client_stack(
        logger, stop_event, resume_event, downloader=_fake_downloader)
    task = asyncio.create_task(client.run())
    try:
        ok = await wait_until(lambda: client._registered, timeout=8)  # noqa: SLF001
        ids = ["T-C1", "T-C2", "T-C3"]
        for tid in ids:
            await server.send_to_client("publish_task", {
                "task_id": tid, "platform_key": "douyin", "content_type": "video",
                "file_url": f"https://example.invalid/{tid}.mp4", "media_urls": [],
                "title": tid, "description": "", "tags": [],
                "account_name": "mock-account", "submit_mode": "auto",
                "scheduled_at": int(time.time() * 1000),
            })
        ok2 = await wait_until(
            lambda: all(_task_status(t) == "success" for t in ids), timeout=20)
        check("3 任务全部执行成功", ok and ok2,
              f"status={[ (t, _task_status(t)) for t in ids ]}")
        check("并发峰值 ≤2（max_concurrency=2）", state["max"] == 2,
              f"峰值并发={state['max']}")
    finally:
        upstream_adapter.unregister_uploader("douyin", "video")
        await stop_client(task, stop_event)
        await server.stop()


# ================================================================ 场景 5


async def scenario_recover_pending() -> None:
    print("\n==== 场景5：重启恢复（recover_pending 扫 queued/running 重新入队）====",
          flush=True)
    fresh_env()
    # 预置两个未完成任务（模拟进程重启前的落库状态）
    for tid, status in (("T-R1", "queued"), ("T-R2", "running")):
        db.upsert_task(tid, json.dumps({
            "task_id": tid, "platform_key": "douyin", "content_type": "video",
            "file_url": f"https://example.invalid/{tid}.mp4", "media_urls": [],
            "title": tid, "description": "", "tags": [],
            "account_name": "mock-account", "submit_mode": "auto",
            "scheduled_at": int(time.time() * 1000),
        }, ensure_ascii=False), status=status)

    results: list[tuple] = []

    async def capture_result(task_id, status, error="", publish_url="", remarks=""):
        results.append((task_id, status))

    async def fake_upload(*, payload, account_file, video_file, image_files):
        return ""

    upstream_adapter.register_uploader("douyin", "video", fake_upload)
    logger, _ = make_logger()
    stop_event = asyncio.Event()
    try:
        dispatcher = TaskDispatcher(
            logger,
            result_sender=capture_result,
            file_renew_sender=lambda tid: _a_true(),
            scheduling_paused=lambda: False,
            stop_event=stop_event,
            max_concurrency=2,
            downloader=_fake_downloader,
        )
        recovered = dispatcher.recover_pending()
        ok = await wait_until(
            lambda: all(_task_status(t) == "success" for t in ("T-R1", "T-R2")),
            timeout=10)
        check("recover_pending 扫到 2 个未完成任务", recovered == 2,
              f"recovered={recovered}")
        check("恢复后全部执行成功并回报结果",
              ok and sorted(results) == [("T-R1", "success"), ("T-R2", "success")],
              f"results={results} status=({_task_status('T-R1')},{_task_status('T-R2')})")
    finally:
        upstream_adapter.unregister_uploader("douyin", "video")
        stop_event.set()


async def _a_true() -> bool:
    return True


# ================================================================ 主入口


async def main() -> int:
    await scenario_success_chain()
    await scenario_file_renew()
    await scenario_cookie_error()
    await scenario_concurrency()
    await scenario_recover_pending()

    failed = [r for r in RESULTS if r.startswith("[FAIL]")]
    report = os.path.join(_HERE, "_verify_report_s4.txt")
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(RESULTS) + f"\n\n总计 {len(RESULTS)} 项，失败 {len(failed)} 项\n")
    print(f"\n总计 {len(RESULTS)} 项，失败 {len(failed)} 项；报告: {report}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
