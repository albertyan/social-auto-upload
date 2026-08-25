# -*- coding: utf-8 -*-
"""S2（Agent WS 核心）场景验证脚本（任务 #13 第二步）。

场景：
1. 注册握手 + 心跳往返 + publish_task 落库 + result_queue 落库/补发；
2. 断线重连退避（1011 异常关闭 → 2s/4s 指数退避）；
3. 4401 挂起零重连 + resume 唤醒后重连；
4. result_queue 先落库后发送、成功后删除（离线积压 → 连接后补发，含 >50 项多批清空）；
5. 服务端主动 1000 关闭（如发版重启）→ 客户端重连而非退出（review 修复项）。

运行（仓库根目录）：
    .venv\\Scripts\\python.exe sau_wrap\\tests\\verify_s2.py

数据隔离：SAU_DATA_ROOT 指向本目录下 _tmpdata（不触碰 %ProgramData%\\SAU）。
退出码：0=全部通过；1=存在失败。报告写 ``_verify_report.txt``（UTF-8）。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _REPO_ROOT)
os.environ["SAU_DATA_ROOT"] = os.path.join(_HERE, "_tmpdata")

from sau_wrap import paths                                    # noqa: E402
from sau_wrap.agent import config as agent_config             # noqa: E402
from sau_wrap.agent import db                                 # noqa: E402
from sau_wrap.agent import ws_client as ws_mod                # noqa: E402
from sau_wrap.tests.mock_ws_server import MockAgentServer     # noqa: E402

RESULTS: list[str] = []
FAKE_MACHINE = "ab" * 16  # 32 位 hex（机器码真实性由 sau machine-code 单独验证）


def check(name: str, cond: bool, evidence: str) -> None:
    status = "PASS" if cond else "FAIL"
    RESULTS.append(f"[{status}] {name} :: {evidence}")
    print(f"[{status}] {name} :: {evidence}", flush=True)


def make_logger() -> tuple[logging.Logger, io.StringIO]:
    buf = io.StringIO()
    logger = logging.getLogger(f"sau.verify.{time.time_ns()}")
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    return logger, buf


def fresh_env() -> None:
    """清理 config/凭证/DB，重建干净环境。"""
    for f in (paths.CONFIG_FILE, paths.CREDENTIAL_FILE, paths.DB_FILE):
        with contextlib.suppress(OSError):
            os.remove(f)
    db.db_init()
    with db._LOCK, db._connect() as conn:  # noqa: SLF001（测试专用）
        conn.execute("DELETE FROM local_tasks")
        conn.execute("DELETE FROM result_queue")


def bind_to(url: str, heartbeat_interval: int = 1) -> None:
    cfg = agent_config.bind(url, "mock_token_" + "x" * 16)
    cfg.heartbeat_interval = heartbeat_interval
    agent_config.save_config(cfg)


async def wait_until(pred, timeout: float, interval: float = 0.1) -> bool:
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


# ================================================================ 场景 1


async def scenario_handshake_heartbeat() -> None:
    print("\n==== 场景1：注册握手 + 心跳往返 + publish_task 落库 ====", flush=True)
    fresh_env()
    server = MockAgentServer()
    await server.start()
    bind_to(server.url, heartbeat_interval=1)
    server.on_connect_actions.append("publish:T-001")

    logger, logbuf = make_logger()
    stop_event, resume_event = asyncio.Event(), asyncio.Event()
    client = ws_mod.WSClient(logger, stop_event, resume_event)
    task = asyncio.create_task(client.run())

    ok = await wait_until(lambda: len(server.received_of("heartbeat")) >= 2, timeout=10)
    regs = server.received_of("register")
    check("register 首包字段", bool(regs) and regs[0]["data"]["agent_id"]
          and len(regs[0]["data"]["machine_code"]) == 32
          and regs[0]["data"]["platforms"] == [] and regs[0]["data"]["accounts"] == [],
          f"register.data={json.dumps(regs[0]['data'], ensure_ascii=False)[:200] if regs else '无'}")
    hbs = server.received_of("heartbeat")
    check("心跳 ≥2 次且含 clock_offset_seconds/active_tasks",
          ok and all("clock_offset_seconds" in m["data"] and "active_tasks" in m["data"] for m in hbs),
          f"收到 {len(hbs)} 次心跳, 样例={json.dumps(hbs[0]['data'], ensure_ascii=False) if hbs else '无'}")
    check("客户端收到 registered 并记录有效期", client._registered,  # noqa: SLF001
          f"registered={client._registered} expire_at={client.expire_at}")

    ok = await wait_until(lambda: _task_in_db("T-001"), timeout=5)
    row = _db_row("SELECT status, payload FROM local_tasks WHERE task_id='T-001'")
    check("publish_task 落 local_tasks", ok and row is not None,
          f"status={row[0] if row else '-'} payload[:120]={row[1][:120] if row else '-'}")

    await stop_client(task, stop_event)
    await server.stop()
    check("优雅停止（日志含 主循环退出）", "主循环退出" in logbuf.getvalue(),
          "日志尾部=" + " | ".join(logbuf.getvalue().strip().splitlines()[-3:]))


def _task_in_db(task_id: str) -> bool:
    return _db_row("SELECT 1 FROM local_tasks WHERE task_id=?", (task_id,)) is not None


def _db_row(sql: str, args: tuple = ()):
    with db._LOCK, db._connect() as conn:
        return conn.execute(sql, args).fetchone()


# ================================================================ 场景 2


async def scenario_backoff() -> None:
    print("\n==== 场景2：断线重连退避（1011 → 2s/4s 指数）====", flush=True)
    fresh_env()
    server = MockAgentServer()
    await server.start()
    bind_to(server.url)
    # 每次连接立即以 1011 关闭（模拟服务端异常）
    server.on_connect_actions.extend(["close:1011"] * 6)

    logger, logbuf = make_logger()
    stop_event, resume_event = asyncio.Event(), asyncio.Event()
    client = ws_mod.WSClient(logger, stop_event, resume_event)
    orig_machine = ws_mod.get_machine_code
    ws_mod.get_machine_code = lambda: FAKE_MACHINE  # 加速重连（剔除机器码采集耗时）
    try:
        task = asyncio.create_task(client.run())
        ok = await wait_until(lambda: server.connections >= 3, timeout=25)
    finally:
        ws_mod.get_machine_code = orig_machine
        await stop_client(task, stop_event)
        await server.stop()

    conn_times = [t for t, ev, _ in server.events if ev == "connect"]
    gaps = [round(b - a, 2) for a, b in zip(conn_times, conn_times[1:])]
    check("观测到 ≥3 次连接尝试", ok and server.connections >= 3,
          f"连接数={server.connections} 间隔={gaps}")
    check("退避间隔递增（≈2s → ≈4s）",
          len(gaps) >= 2 and 1.5 <= gaps[0] <= 3.5 and 3.2 <= gaps[1] <= 6.5,
          f"间隔={gaps}（允许机器/调度抖动）")


# ================================================================ 场景 3


async def scenario_4401_suspend() -> None:
    print("\n==== 场景3：4401 挂起零重连 + 热重载唤醒 ====", flush=True)
    fresh_env()
    server = MockAgentServer()
    await server.start()
    bind_to(server.url)
    server.on_connect_actions.append("close:4401")  # 仅首个会话被踢

    logger, logbuf = make_logger()
    stop_event, resume_event = asyncio.Event(), asyncio.Event()
    client = ws_mod.WSClient(logger, stop_event, resume_event)
    orig_machine = ws_mod.get_machine_code
    ws_mod.get_machine_code = lambda: FAKE_MACHINE
    try:
        task = asyncio.create_task(client.run())
        ok1 = await wait_until(lambda: server.connections >= 1, timeout=8)
        await asyncio.sleep(6)  # 挂起窗口：期间不得重连
        suspended_no_retry = ok1 and server.connections == 1
        check("4401 后挂起零重连（6s 内无新连接）", suspended_no_retry,
              f"连接数={server.connections} 日志含'挂起'={'挂起' in logbuf.getvalue()}")

        resume_event.set()  # 模拟配置热重载唤醒
        regs_before = len(server.received_of("register"))
        ok2 = await wait_until(
            lambda: len(server.received_of("register")) > regs_before, timeout=8)
        check("resume 唤醒后重新连接并注册",
              ok2 and server.connections >= 2,
              f"连接数={server.connections} register 次数 {regs_before}→{len(server.received_of('register'))}")
    finally:
        ws_mod.get_machine_code = orig_machine
        await stop_client(task, stop_event)
        await server.stop()


# ================================================================ 场景 4


async def scenario_result_queue() -> None:
    print("\n==== 场景4：result_queue 先落库 → 发送成功删除（离线积压补发）====", flush=True)
    fresh_env()
    # 离线状态先落两条结果（模拟断线期间产生）
    id1 = db.enqueue_result("T-100", json.dumps(
        {"task_id": "T-100", "status": "success", "error": "",
         "publish_url": "https://example.invalid/v1", "remarks": ""}, ensure_ascii=False))
    id2 = db.enqueue_result("T-101", json.dumps(
        {"task_id": "T-101", "status": "failed", "error": "下载素材失败",
         "publish_url": "", "remarks": ""}, ensure_ascii=False))
    rows = db.peek_results()
    check("离线落库成功（result_queue 2 项）", len(rows) == 2,
          f"queue={[(r[0], r[1]) for r in rows]} id=({id1},{id2})")

    server = MockAgentServer()
    await server.start()
    bind_to(server.url)

    logger, logbuf = make_logger()
    stop_event, resume_event = asyncio.Event(), asyncio.Event()
    client = ws_mod.WSClient(logger, stop_event, resume_event)
    task = asyncio.create_task(client.run())

    ok = await wait_until(lambda: len(server.received_of("task_result")) >= 2, timeout=10)
    results = server.received_of("task_result")
    check("连接后补发两条 task_result", ok and len(results) == 2,
          f"服务端收到={[(m['data']['task_id'], m['data']['status']) for m in results]}")
    check("补发成功后队列清空", len(db.peek_results()) == 0,
          f"剩余队列={db.peek_results()}")

    await stop_client(task, stop_event)
    await server.stop()

    # —— 多批清空（>50 项，验证 while 循环逐批补发而非单次上限）
    with db._LOCK, db._connect() as conn:  # noqa: SLF001（测试专用）
        conn.execute("DELETE FROM result_queue")
    for i in range(60):
        db.enqueue_result(f"T-{i:03d}", json.dumps(
            {"task_id": f"T-{i:03d}", "status": "success", "error": "",
             "publish_url": "", "remarks": ""}, ensure_ascii=False))
    server2 = MockAgentServer()
    await server2.start()
    bind_to(server2.url)
    logger2, _ = make_logger()
    stop2, resume2 = asyncio.Event(), asyncio.Event()
    client2 = ws_mod.WSClient(logger2, stop2, resume2)
    task2 = asyncio.create_task(client2.run())
    ok = await wait_until(lambda: len(server2.received_of("task_result")) >= 60, timeout=20)
    check("60 项积压多批补发全部送达（>50 单次上限）",
          ok and len(db.peek_results()) == 0,
          f"服务端收到={len(server2.received_of('task_result'))} 剩余队列={len(db.peek_results())}")
    await stop_client(task2, stop2)
    await server2.stop()


# ================================================================ 场景 5（新增）


async def scenario_server_1000_reconnect() -> None:
    print("\n==== 场景5：服务端主动 1000 关闭 → 重连而非退出（review 修复）====", flush=True)
    fresh_env()
    server = MockAgentServer()
    await server.start()
    bind_to(server.url)

    logger, logbuf = make_logger()
    stop_event, resume_event = asyncio.Event(), asyncio.Event()
    client = ws_mod.WSClient(logger, stop_event, resume_event)
    orig_machine = ws_mod.get_machine_code
    ws_mod.get_machine_code = lambda: FAKE_MACHINE
    try:
        task = asyncio.create_task(client.run())
        ok = await wait_until(lambda: len(server.received_of("register")) >= 1, timeout=8)
        assert ok, "首个会话未建立"
        await server.close_current(1000, "server restart")  # 服务端发版重启式关闭
        ok2 = await wait_until(
            lambda: len(server.received_of("register")) >= 2, timeout=10)
        alive = not task.done()
        check("1000 关闭后客户端重连并重新注册（未退出进程）",
              ok2 and alive and server.connections >= 2,
              f"连接数={server.connections} register 次数={len(server.received_of('register'))} 主循环存活={alive}")
    finally:
        ws_mod.get_machine_code = orig_machine
        await stop_client(task, stop_event)
        await server.stop()


# ================================================================ main


async def amain() -> int:
    await scenario_handshake_heartbeat()
    await scenario_backoff()
    await scenario_4401_suspend()
    await scenario_result_queue()
    await scenario_server_1000_reconnect()
    failed = [r for r in RESULTS if r.startswith("[FAIL]")]
    print(f"\n==== 汇总：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ====", flush=True)
    return 1 if failed else 0


def main() -> int:
    code = asyncio.run(amain())
    report = os.path.join(_HERE, "_verify_report.txt")
    with open(report, "w", encoding="utf-8") as f:
        f.write(f"S2 验证报告 @ {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("\n".join(RESULTS) + "\n")
    print(f"报告已写入: {report}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
