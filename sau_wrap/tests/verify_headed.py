# -*- coding: utf-8 -*-
"""任务 #6 补充：有头登录覆盖缺口补齐（假注入风格，同 verify_headed_lite）。

verify_headed_lite（15 项）已覆盖：503、令牌鉴权 403、404/409 守卫、
拉起失败降级文案、取消唤醒。本文件补齐两处缺口：

1. 状态机失败分支：子进程回报 ``{"ok": false}`` → 会话 failed +
   message 透传 + result_event 置位唤醒执行器 + 终态再回报 409；
2. headed 会话超时收敛：短超时（1.5s）下无回报 → timeout 终态，
   迟到回报 → 409 ``session_terminal``（不覆盖）。

运行：``.venv\\Scripts\\python.exe sau_wrap\\tests\\verify_headed.py``
数据隔离：``SAU_DATA_ROOT`` → 本目录 ``_tmpdata_headed2``。退出码 0=全部通过。
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import socket
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _REPO_ROOT)
os.environ["SAU_DATA_ROOT"] = os.path.join(_HERE, "_tmpdata_headed2")

import aiohttp                                          # noqa: E402
from sau_wrap.service import headed_launcher           # noqa: E402
from sau_wrap.service import login_sessions as ls      # noqa: E402
from sau_wrap.service.local_api import LocalApiServer   # noqa: E402

RESULTS: list[str] = []


def check(name: str, cond: bool, evidence: str) -> None:
    status = "PASS" if cond else "FAIL"
    RESULTS.append(f"[{status}] {name} :: {evidence}")
    print(f"[{status}] {name} :: {evidence}", flush=True)


def make_logger() -> logging.Logger:
    logger = logging.getLogger(f"sau.verify_headed2.{time.time_ns()}")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(logging.StreamHandler(io.StringIO()))
    return logger


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeClient:
    def __init__(self) -> None:
        self.sync_calls = 0

    async def send_account_sync(self) -> bool:
        self.sync_calls += 1
        return True


async def main() -> int:
    logger = make_logger()
    port = free_port()
    client = FakeClient()

    # 与 verify_headed_lite 同构：headed 走真实 headed_real_executor（拉起器被 patch），
    # headless 分支挂起（本补充用例不触达）。
    async def fake_exec(session: ls.LoginSession) -> None:
        if session.mode == "headed":
            return await ls.headed_real_executor(session)
        await asyncio.sleep(300)

    # 短超时（1.5s）：缺口②超时收敛实测可控时长
    mgr = ls.LoginSessionManager(logger, executor=fake_exec,
                                 browser_check=lambda: True, timeout=1.5)
    api = LocalApiServer(logger, client, port, login_manager=mgr)

    orig_find = headed_launcher.find_interactive_user
    orig_launch = headed_launcher.launch_headed
    headed_launcher.find_interactive_user = lambda: "tester"
    headed_launcher.launch_headed = lambda p, a, s: (True, "MANUAL_CMD_OK")
    await api.start()
    base = f"http://127.0.0.1:{port}"
    headers = {"X-SAU-Local-Token": api.token}
    try:
        async with aiohttp.ClientSession() as sess:
            # ---- 缺口①：回报 ok=false → failed + message 透传 + 唤醒执行器
            r1 = await sess.post(f"{base}/login/douyin", headers=headers,
                                 json={"mode": "headed", "account_name": "accF2"})
            b1 = await r1.json()
            sid = b1["session_id"]
            s1 = mgr.get(sid)
            check("前置：headed 会话创建成功（waiting 待回报）",
                  r1.status == 200 and b1["status"] == "waiting",
                  f"body={b1}")

            r2 = await sess.post(f"{base}/login/headed/result", headers=headers,
                                 json={"session_id": sid, "ok": False,
                                       "message": "用户关闭了登录窗口"})
            b2 = await r2.json()
            # 等待执行器被唤醒收敛
            for _ in range(50):
                if s1.task is not None and s1.task.done():
                    break
                await asyncio.sleep(0.05)
            check("回报 ok=false → failed + message 透传 + 执行器唤醒（任务结束）",
                  r2.status == 200 and b2 == {"ok": True}
                  and s1.status == "failed"
                  and s1.message == "用户关闭了登录窗口"
                  and s1.result_event.is_set() and s1.task.done(),
                  f"report={b2} status={s1.status} msg={s1.message!r} "
                  f"task_done={s1.task.done()}")

            r3 = await sess.post(f"{base}/login/headed/result", headers=headers,
                                 json={"session_id": sid, "ok": True})
            check("failed 终态再回报 → 409 幂等（不覆盖为 success）",
                  r3.status == 409 and mgr.get(sid).status == "failed",
                  f"status={r3.status} final={mgr.get(sid).status}")

            # ---- 缺口②：无回报 → 会话超时收敛（外层 wait_for 兜底）
            r4 = await sess.post(f"{base}/login/kuaishou", headers=headers,
                                 json={"mode": "headed", "account_name": "accT2"})
            b4 = await r4.json()
            sid_t = b4["session_id"]
            s4 = mgr.get(sid_t)
            check("前置：第二个 headed 会话创建成功（waiting）",
                  r4.status == 200 and b4["status"] == "waiting",
                  f"body={b4}")

            deadline = time.time() + 5.0
            while time.time() < deadline and s4.status != "timeout":
                await asyncio.sleep(0.1)
            check("无回报 → 会话超时收敛（timeout 终态 + 文案）",
                  s4.status == "timeout" and "超时" in s4.message
                  and s4.task.done(),
                  f"status={s4.status} message={s4.message!r} "
                  f"task_done={s4.task.done()}")

            r5 = await sess.post(f"{base}/login/headed/result", headers=headers,
                                 json={"session_id": sid_t, "ok": True})
            b5 = await r5.json()
            check("超时后迟到回报 → 409 session_terminal（不覆盖 timeout）",
                  r5.status == 409 and b5.get("error") == "session_terminal"
                  and mgr.get(sid_t).status == "timeout",
                  f"status={r5.status} body={b5}")
    finally:
        headed_launcher.find_interactive_user = orig_find
        headed_launcher.launch_headed = orig_launch
        await mgr.close_all()
        await api.stop()

    total = len(RESULTS)
    failed = sum(1 for r in RESULTS if r.startswith("[FAIL]"))
    print(f"\n==== 总计：{total - failed}/{total} 通过 ====", flush=True)
    report = os.path.join(_HERE, "_verify_report_headed.txt")
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(RESULTS))
    print(f"报告已写入: {report}", flush=True)
    return failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
