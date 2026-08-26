# -*- coding: utf-8 -*-
"""任务 #4（有头登录后端能力）轻量自测（非完整真机验证）。

**全假注入，不拉真实计划任务/浏览器**：monkeypatch
``headed_launcher.launch_headed`` / ``find_interactive_user``，
覆盖服务侧新链路的状态机与 HTTP 契约：

1. ``POST /login/{platform}`` mode=headed：
   无交互会话 → 503 ``no_interactive_session``；mode 非法 → 400；
   拉起失败不拒绝创建 → failed + message 含手动命令；
2. ``to_status_dict`` 含 ``mode`` 字段；
3. ``POST /login/headed/result``：令牌鉴权（Cookie → 403）、
   会话不存在 → 404、非 headed 会话 → 409、终态幂等 → 409、
   成功回报 → success + account_sync；
4. headed 会话取消：置终态 + 置位事件唤醒执行器 → cancelled；
5. ``headed_launcher`` 单元面：任务 XML 转义/结构、手动命令生成、
   session_id 白名单拒绝。

运行：``.venv\\Scripts\\python.exe sau_wrap\\tests\\verify_headed_lite.py``
数据隔离：``SAU_DATA_ROOT`` → 本目录 ``_tmpdata_headed``。退出码 0=全部通过。
完整真机链路（schtasks 投放/桌面窗口/回报）由后续验证任务补 ``verify_headed.py``。
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
os.environ["SAU_DATA_ROOT"] = os.path.join(_HERE, "_tmpdata_headed")

import aiohttp                                          # noqa: E402
from sau_wrap.service import headed_launcher           # noqa: E402
from sau_wrap.service import login_sessions as ls      # noqa: E402
from sau_wrap.service.local_api import LocalApiServer   # noqa: E402

RESULTS: list[str] = []


def check(name: str, cond: bool, evidence: str) -> None:
    status = "PASS" if cond else "FAIL"
    RESULTS.append(f"[{status}] {name} :: {evidence}")
    print(f"[{status}] {name} :: {evidence}", flush=True)


def make_logger() -> tuple[logging.Logger, io.StringIO]:
    buf = io.StringIO()
    logger = logging.getLogger(f"sau.verify_headed.{time.time_ns()}")
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    return logger, buf


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


# ================================================================ 场景 1：单元面


def scenario_launcher_units() -> None:
    print("\n==== 场景1：headed_launcher 单元面 ====", flush=True)
    user = headed_launcher.find_interactive_user()
    check("find_interactive_user 任何情况返回 str|None 不抛异常",
          user is None or isinstance(user, str), f"user={user!r}")

    xml = headed_launcher._task_xml(r"C:\Program Files\SAU\sau.exe",
                                    'login-headed --platform douyin '
                                    '--account a&b "<c>"')
    check("任务 XML：InteractiveToken + 参数转义（& < > \"）",
          "<LogonType>InteractiveToken</LogonType>" in xml
          and "&amp;" in xml and "&lt;c&gt;" in xml
          and "--platform douyin" in xml,
          f"len={len(xml)}")

    cmd = headed_launcher.build_manual_command("douyin", "acc1", "sid-01")
    check("手动命令生成（含 login-headed 与全部参数；路径带引号规则自洽）",
          "login-headed" in cmd and "--platform douyin" in cmd
          and "--account acc1" in cmd and "--session-id sid-01" in cmd,
          f"cmd={cmd}")

    ok, _ = headed_launcher.launch_headed("douyin", "acc", "../evil\\sid")
    check("session_id 白名单拒绝（非法 → 不构造任务，返回失败）", ok is False,
          f"ok={ok}")


# ================================================================ 场景 2：HTTP 链路


async def scenario_http() -> None:
    print("\n==== 场景2：有头登录 HTTP 端点链路（假拉起器） ====", flush=True)
    logger, logbuf = make_logger()
    port = free_port()
    client = FakeClient()

    # 与 default_real_executor 同构的模式分发，但 headless 分支挂起不拉真实浏览器；
    # headed 分支走真实 headed_real_executor（拉起器已被 patch）。
    async def fake_exec(session: ls.LoginSession) -> None:
        if session.mode == "headed":
            return await ls.headed_real_executor(session)
        await asyncio.sleep(300)

    mgr = ls.LoginSessionManager(logger, executor=fake_exec,
                                 browser_check=lambda: True)
    api = LocalApiServer(logger, client, port, login_manager=mgr)

    # 假交互会话探测 + 假拉起器（默认成功；失败场景切换返回值）
    interactive = {"user": "tester"}
    launch_ret = {"value": (True, "MANUAL_CMD_FALLBACK")}
    orig_find = headed_launcher.find_interactive_user
    orig_launch = headed_launcher.launch_headed
    headed_launcher.find_interactive_user = lambda: interactive["user"]
    headed_launcher.launch_headed = lambda p, a, s: launch_ret["value"]
    await api.start()
    base = f"http://127.0.0.1:{port}"
    headers = {"X-SAU-Local-Token": api.token}
    try:
        async with aiohttp.ClientSession() as sess:
            # —— mode 非法 → 400
            r0 = await sess.post(f"{base}/login/douyin", headers=headers,
                                 json={"mode": "weird"})
            b0 = await r0.json()
            check("mode 非法 → 400 invalid_mode",
                  r0.status == 400 and b0.get("error") == "invalid_mode",
                  f"status={r0.status} body={b0}")

            # —— 无交互会话 → 503 no_interactive_session
            interactive["user"] = None
            r1 = await sess.post(f"{base}/login/douyin", headers=headers,
                                 json={"mode": "headed"})
            b1 = await r1.json()
            interactive["user"] = "tester"
            check("headed 且无活跃桌面会话 → 503 no_interactive_session",
                  r1.status == 503 and b1.get("error") == "no_interactive_session"
                  and b1.get("message"),
                  f"status={r1.status} body={b1}")

            # —— 拉起失败不拒绝创建 → failed + 手动命令引导
            launch_ret["value"] = (False, "MANUAL_CMD_FALLBACK")
            r2 = await sess.post(f"{base}/login/kuaishou", headers=headers,
                                 json={"mode": "headed", "account_name": "accF"})
            b2 = await r2.json()
            check("headed 创建成功且响应含 mode 字段（拉起失败不拒绝创建）",
                  r2.status == 200 and b2.get("mode") == "headed"
                  and b2.get("status") == "waiting",
                  f"status={r2.status} body={b2}")
            sid_f = b2["session_id"]
            for _ in range(50):
                if mgr.get(sid_f).is_terminal():
                    break
                await asyncio.sleep(0.05)
            sf = mgr.get(sid_f)
            check("拉起失败 → failed 且 message 含手动命令引导",
                  sf.status == "failed" and "MANUAL_CMD_FALLBACK" in sf.message,
                  f"status={sf.status} message={sf.message[:80]}")

            # —— 拉起成功 → 等待回报；结果回报端点各分支
            launch_ret["value"] = (True, "MANUAL_CMD_OK")
            r3 = await sess.post(f"{base}/login/douyin", headers=headers,
                                 json={"mode": "headed", "account_name": "accH"})
            b3 = await r3.json()
            sid = b3["session_id"]
            check("headed 会话创建（拉起成功，状态 waiting 待回报）",
                  r3.status == 200 and b3["mode"] == "headed"
                  and b3["status"] == "waiting",
                  f"body={b3}")

            # Cookie 会话鉴权回报 → 403（仅令牌；经票据链路换真实会话 Cookie）
            rt = await sess.post(f"{base}/ui-ticket", headers=headers)
            ticket = (await rt.json()).get("ticket", "")
            rx = await sess.get(f"{base}/ui/t/{ticket}", allow_redirects=False)
            cookie = rx.cookies.get("sau_session")
            cookie_hdr = {"Cookie": f"sau_session={cookie.value}"} if cookie else {}
            rc = await sess.post(f"{base}/login/headed/result",
                                 headers=cookie_hdr,
                                 json={"session_id": sid, "ok": True})
            check("结果回报：Cookie 会话鉴权 → 403（仅令牌可回报）",
                  rc.status == 403, f"status={rc.status}")

            # 会话不存在 → 404；非 headed 会话 → 409
            r4 = await sess.post(f"{base}/login/headed/result", headers=headers,
                                 json={"session_id": "no-such", "ok": True})
            r5h = await sess.post(f"{base}/login/tencent", headers=headers, json={})
            sid_hl = (await r5h.json())["session_id"]
            r5 = await sess.post(f"{base}/login/headed/result", headers=headers,
                                 json={"session_id": sid_hl, "ok": True})
            b5 = await r5.json()
            check("结果回报守卫：会话不存在 404 / 非 headed 会话 409",
                  r4.status == 404 and r5.status == 409
                  and b5.get("error") == "not_headed_session",
                  f"status={(r4.status, r5.status)} body5={b5}")

            # 正确回报 → {"ok": true} + 会话 success + account_sync 触发
            r6 = await sess.post(f"{base}/login/headed/result", headers=headers,
                                 json={"session_id": sid, "ok": True,
                                       "message": "扫码成功"})
            b6 = await r6.json()
            rs = await sess.get(f"{base}/login/status/{sid}", headers=headers)
            bs = await rs.json()
            check("结果回报成功 → 会话 success + mode=headed + account_sync",
                  r6.status == 200 and b6 == {"ok": True}
                  and bs["status"] == "success" and bs["mode"] == "headed"
                  and client.sync_calls >= 1,
                  f"report={b6} status={bs['status']} sync={client.sync_calls}")

            # 终态再回报 → 409 幂等忽略
            r7 = await sess.post(f"{base}/login/headed/result", headers=headers,
                                 json={"session_id": sid, "ok": False,
                                       "message": "迟到回报"})
            b7 = await r7.json()
            check("终态再回报 → 409 幂等忽略（不覆盖结果）",
                  r7.status == 409 and b7.get("error") == "session_terminal",
                  f"status={r7.status} body={b7}")

            # —— headed 会话取消：置终态 + 置位事件唤醒执行器
            r8 = await sess.post(f"{base}/login/xiaohongshu", headers=headers,
                                 json={"mode": "headed"})
            sid8 = (await r8.json())["session_id"]
            r9 = await sess.delete(f"{base}/login/{sid8}", headers=headers)
            b9 = await r9.json()
            s8 = mgr.get(sid8)
            check("headed 会话取消 → cancelled 且执行器被唤醒（任务结束）",
                  r9.status == 200 and b9["status"] == "cancelled"
                  and s8.task.done() and s8.result_event.is_set(),
                  f"cancel={b9} task_done={s8.task.done()}")

            # 清理 tencent headless 会话
            await sess.delete(f"{base}/login/{sid_hl}", headers=headers)

            audit = [ln for ln in logbuf.getvalue().splitlines() if "[AUDIT]" in ln]
            check("有头族审计（login_start mode=headed / login_headed_result / 503）",
                  any("op=login_headed_result" in ln for ln in audit)
                  and any("mode=headed" in ln for ln in audit)
                  and any("no_interactive_session" in ln for ln in audit),
                  f"audit_lines={len(audit)}")
    finally:
        headed_launcher.find_interactive_user = orig_find
        headed_launcher.launch_headed = orig_launch
        await mgr.close_all()
        await api.stop()


async def main() -> int:
    scenario_launcher_units()
    await scenario_http()
    total = len(RESULTS)
    failed = sum(1 for r in RESULTS if r.startswith("[FAIL]"))
    print(f"\n==== 总计：{total - failed}/{total} 通过 ====", flush=True)
    report = os.path.join(_HERE, "_verify_report_headed_lite.txt")
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(RESULTS))
    print(f"报告已写入: {report}", flush=True)
    return failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
