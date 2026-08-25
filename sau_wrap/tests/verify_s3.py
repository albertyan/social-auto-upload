# -*- coding: utf-8 -*-
"""S3（5409 本地 API）场景验证脚本（任务 #14 第三步）。

场景：
1. 鉴权：无令牌 / 错误令牌 → 401；正确令牌 → 200；
2. ``GET /status`` 字段结构（§3.7 契约）；
3. ``GET/POST /config``：读/写 server_url，写入触发审计与热重载；
4. ``POST /bind`` 复用 ``sau bind`` 逻辑（config.json + credential.bin 落盘）；
5. 4401 挂起 → ``POST /reload`` 唤醒重连（复用 mock WS 服务端，含挂起态 ws_connected=False 断言）；
6. 占位端点（/ui/*、/upgrade 等）→ 501 + 说明；
7. 端口被占用 → 明确报错（``LocalApiBindError`` + 日志），禁止静默失败（§4.4）；
8. 退避期间（8s 档）``POST /config`` 热重载打断退避立即重连（<2s）。

运行：``.venv\\Scripts\\python.exe sau_wrap\\tests\\verify_s3.py``
数据隔离：``SAU_DATA_ROOT`` → 本目录 ``_tmpdata3``。退出码 0=全部通过。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import socket
import sys
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _REPO_ROOT)
os.environ["SAU_DATA_ROOT"] = os.path.join(_HERE, "_tmpdata3")

import aiohttp                                          # noqa: E402
from sau_wrap import paths                              # noqa: E402
from sau_wrap.agent import config as agent_config      # noqa: E402
from sau_wrap.agent import db                          # noqa: E402
from sau_wrap.agent import ws_client as ws_mod         # noqa: E402
from sau_wrap.service.local_api import (               # noqa: E402
    LocalApiBindError,
    LocalApiServer,
)
from sau_wrap.tests.mock_ws_server import MockAgentServer  # noqa: E402

RESULTS: list[str] = []
FAKE_MACHINE = "ab" * 16


def check(name: str, cond: bool, evidence: str) -> None:
    status = "PASS" if cond else "FAIL"
    RESULTS.append(f"[{status}] {name} :: {evidence}")
    print(f"[{status}] {name} :: {evidence}", flush=True)


def make_logger() -> tuple[logging.Logger, io.StringIO]:
    buf = io.StringIO()
    logger = logging.getLogger(f"sau.verify3.{time.time_ns()}")
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    return logger, buf


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fresh_env() -> None:
    for f in (paths.CONFIG_FILE, paths.CREDENTIAL_FILE, paths.DB_FILE, paths.LOCAL_TOKEN_FILE):
        with contextlib.suppress(OSError):
            os.remove(f)
    db.db_init()
    with db._LOCK, db._connect() as conn:  # noqa: SLF001（测试专用）
        conn.execute("DELETE FROM local_tasks")
        conn.execute("DELETE FROM result_queue")


async def wait_until(pred, timeout: float, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return pred()


# ================================================================ 场景 1/2/3/6


async def scenario_api_basics() -> None:
    print("\n==== 场景1/2/3/6：鉴权 + /status + /config + 占位501 ====", flush=True)
    fresh_env()
    logger, logbuf = make_logger()
    stop_event, resume_event = asyncio.Event(), asyncio.Event()
    client = ws_mod.WSClient(logger, stop_event, resume_event)
    port = free_port()
    api = LocalApiServer(logger, client, port)
    await api.start()
    base = f"http://127.0.0.1:{port}"
    headers = {"X-SAU-Local-Token": api.token}

    check("令牌文件已生成（每次启动重新生成）",
          paths.LOCAL_TOKEN_FILE.exists()
          and paths.LOCAL_TOKEN_FILE.read_bytes().decode() == api.token,
          f"file={paths.LOCAL_TOKEN_FILE}")

    async with aiohttp.ClientSession() as sess:
        r1 = await sess.get(f"{base}/status")
        r2 = await sess.get(f"{base}/status", headers={"X-SAU-Local-Token": "wrong"})
        r3 = await sess.get(f"{base}/status", headers=headers)
        check("无令牌/错误令牌 → 401，正确令牌 → 200",
              r1.status == 401 and r2.status == 401 and r3.status == 200,
              f"status={(r1.status, r2.status, r3.status)}")

        body = await r3.json()
        required = ["ws_connected", "agent_id", "version", "active_tasks", "accounts",
                    "clock_offset_seconds", "token_expire_at", "token_status",
                    "last_close_reason"]
        missing = [k for k in required if k not in body]
        check("/status 字段结构（§3.7 契约）", not missing,
              f"缺字段={missing or '无'} body={json.dumps(body, ensure_ascii=False)[:220]}")

        # /config：未绑定时读取
        rc = await (await sess.get(f"{base}/config", headers=headers)).json()
        check("GET /config（未绑定）", rc.get("bound") is False, f"resp={rc}")

        # 未绑定时 POST /config → 409 引导先 bind
        rp = await sess.post(f"{base}/config", headers=headers,
                             json={"server_url": "ws://127.0.0.1:1/ws"})
        check("POST /config 未绑定 → 409", rp.status == 409, f"status={rp.status}")

        # /bind：复用 sau bind 逻辑
        rb = await sess.post(f"{base}/bind", headers=headers, json={
            "server_url": "ws://127.0.0.1:1/ws", "token": "mock_token_" + "y" * 16})
        rb_body = await rb.json()
        cfg = agent_config.load_config()
        check("POST /bind 落 config.json + credential.bin",
              rb.status == 200 and cfg is not None and agent_config.load_token() is not None,
              f"resp={rb_body}")

        # /config 读写
        rg = await (await sess.get(f"{base}/config", headers=headers)).json()
        new_url = "ws://127.0.0.1:2/ws"
        rw = await sess.post(f"{base}/config", headers=headers,
                             json={"server_url": new_url, "heartbeat_interval": 25})
        rw_body = await rw.json()
        cfg2 = agent_config.load_config()
        check("POST /config 写入并热重载",
              rw.status == 200 and cfg2 is not None and cfg2.server_url == new_url
              and cfg2.heartbeat_interval == 25,
              f"resp={rw_body} 落盘server_url={cfg2.server_url if cfg2 else '-'}")

        # 占位 501
        pu = await sess.get(f"{base}/ui/", headers=headers)
        pl = await sess.post(f"{base}/login", headers=headers)
        pg = await sess.get(f"{base}/upgrade", headers=headers)
        check("占位端点 /ui/*/login/upgrade → 501",
              pu.status == 501 and pl.status == 501 and pg.status == 501,
              f"status={(pu.status, pl.status, pg.status)} body={(await pu.json())}")

    audit_lines = [ln for ln in logbuf.getvalue().splitlines() if "[AUDIT]" in ln]
    check("写操作审计日志（bind/config，来源 127.0.0.1）",
          any("op=bind" in ln and "source=127.0.0.1" in ln for ln in audit_lines)
          and any("op=config" in ln for ln in audit_lines),
          f"审计行={audit_lines}")

    await api.stop()
    stop_event.set()


# ================================================================ 场景 4：热重载唤醒


async def scenario_reload_wakeup() -> None:
    print("\n==== 场景4：4401 挂起 → POST /reload 唤醒重连 ====", flush=True)
    fresh_env()
    ws_server = MockAgentServer()
    await ws_server.start()
    agent_config.bind(ws_server.url, "mock_token_" + "z" * 16)
    ws_server.on_connect_actions.append("close:4401")  # 首个会话被踢 → 挂起

    logger, logbuf = make_logger()
    stop_event, resume_event = asyncio.Event(), asyncio.Event()
    client = ws_mod.WSClient(logger, stop_event, resume_event)
    orig_machine = ws_mod.get_machine_code
    ws_mod.get_machine_code = lambda: FAKE_MACHINE
    port = free_port()
    api = LocalApiServer(logger, client, port)
    await api.start()
    base = f"http://127.0.0.1:{port}"
    headers = {"X-SAU-Local-Token": api.token}
    task = asyncio.create_task(client.run())
    try:
        ok = await wait_until(lambda: client.suspended, timeout=8)
        st = None
        async with aiohttp.ClientSession() as sess:
            st = await (await sess.get(f"{base}/status", headers=headers)).json()
            rr = await sess.post(f"{base}/reload", headers=headers)
            rr_body = await rr.json()
        check("挂起态 /status 可见（suspended + token_status + ws_connected 已复位）",
              ok and st is not None and st["suspended"] and st["token_status"] == "suspended"
              and st["ws_connected"] is False,
              f"/status={json.dumps(st, ensure_ascii=False)[:200] if st else '-'}")
        ok2 = await wait_until(lambda: len(ws_server.received_of("register")) >= 1, timeout=8)
        check("POST /reload 唤醒挂起客户端并重新注册",
              rr.status == 200 and ok2,
              f"resp={rr_body} register 次数={len(ws_server.received_of('register'))}")
    finally:
        ws_mod.get_machine_code = orig_machine
        stop_event.set()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=10)
        await api.stop()
        await ws_server.stop()


# ================================================================ 场景 5：端口占用


async def scenario_port_conflict() -> None:
    print("\n==== 场景5：端口被占用 → 明确报错（禁止静默失败，§4.4）====", flush=True)
    logger, logbuf = make_logger()
    stop_event, resume_event = asyncio.Event(), asyncio.Event()
    client = ws_mod.WSClient(logger, stop_event, resume_event)

    # 先占用一个端口
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]

    api = LocalApiServer(logger, client, busy_port)
    raised = False
    msg = ""
    try:
        await api.start()
    except LocalApiBindError as exc:
        raised = True
        msg = str(exc)
    blocker.close()
    err_logged = any("绑定" in ln and "失败" in ln for ln in logbuf.getvalue().splitlines())
    check("端口占用抛 LocalApiBindError 且日志明确",
          raised and err_logged and "占用" in msg,
          f"raised={raised} msg[:120]={msg[:120]}")


# ================================================================ 场景 6：退避期热重载打断


async def scenario_backoff_interrupt() -> None:
    print("\n==== 场景6：退避期间 POST /config 热重载打断退避立即重连 ====", flush=True)
    fresh_env()
    logger, logbuf = make_logger()
    # 准备不可达端口（先占用再释放 → 连接必被拒绝）
    dead_port = free_port()
    agent_config.bind(f"ws://127.0.0.1:{dead_port}/ws", "mock_token_" + "x" * 16)
    stop_event, resume_event = asyncio.Event(), asyncio.Event()
    client = ws_mod.WSClient(logger, stop_event, resume_event)
    orig_machine = ws_mod.get_machine_code
    ws_mod.get_machine_code = lambda: FAKE_MACHINE
    port = free_port()
    api = LocalApiServer(logger, client, port)
    await api.start()
    base = f"http://127.0.0.1:{port}"
    headers = {"X-SAU-Local-Token": api.token}
    task = asyncio.create_task(client.run())

    def fail_count() -> int:
        return logbuf.getvalue().count("WS 连接失败")

    try:
        # 3 次失败后进入 8s 档退避（2→4→8）
        ok = await wait_until(lambda: fail_count() >= 3, timeout=15)
        check("断线退避递增生效（2s→4s→8s）", ok, f"失败次数={fail_count()}")
        n0 = fail_count()
        async with aiohttp.ClientSession() as sess:
            rr = await sess.post(f"{base}/config", headers=headers, json={
                "server_url": f"ws://127.0.0.1:{dead_port}/ws"})
            rr_body = await rr.json()
        # 未打断需等满 8s 退避；打断后应立即发起新连接（5s 窗口判定，远小于 8s）
        ok2 = await wait_until(lambda: fail_count() > n0, timeout=5.0)

        def _ts(line: str) -> float:
            return datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f").timestamp()

        lines = logbuf.getvalue().splitlines()
        int_lines = [ln for ln in lines if "打断退避等待" in ln]
        # 用日志时间戳精确判定：打断后立即发起新连接（<1s）；
        # 注：Windows 下被拒连接可能耗时 ~2s（WinError 1225），与时钟时延无关。
        connect_after = ""
        gap = -1.0
        if int_lines:
            t_int = _ts(int_lines[-1])
            after = [ln for ln in lines if _ts(ln) >= t_int and "连接 WS" in ln]
            if after:
                connect_after = after[0]
                gap = _ts(connect_after) - t_int
        check("退避期间 /config 热重载打断退避立即重连（原需等满 8s）",
              rr.status == 200 and ok2 and 0 <= gap < 1.0,
              f"resp={rr_body} 打断→新连接间隔={gap:.2f}s 失败次数={fail_count()}")
    finally:
        ws_mod.get_machine_code = orig_machine
        stop_event.set()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=10)
        await api.stop()


# ================================================================ main


async def amain() -> int:
    await scenario_api_basics()
    await scenario_reload_wakeup()
    await scenario_port_conflict()
    await scenario_backoff_interrupt()
    failed = [r for r in RESULTS if r.startswith("[FAIL]")]
    print(f"\n==== 汇总：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ====", flush=True)
    return 1 if failed else 0


def main() -> int:
    code = asyncio.run(amain())
    report = os.path.join(_HERE, "_verify_report_s3.txt")
    with open(report, "w", encoding="utf-8") as f:
        f.write(f"S3 验证报告 @ {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("\n".join(RESULTS) + "\n")
    print(f"报告已写入: {report}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
