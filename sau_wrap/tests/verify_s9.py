# -*- coding: utf-8 -*-
"""S9（登录扫码会话链路）场景验证脚本（任务 #21 第九步；设计文档 §6.5）。

**全假注入，不真实打开平台页面**（任务禁止项）：``LoginSessionManager``
注入假执行器（脚本式模拟二维码/验证码/成功/失败），``browser_check`` 注入
恒定值；真实 ``default_real_executor`` 的平台分发与上游适配留待真机回归。

场景：
1. 管理器单元语义：平台不支持（400 语义）/ 内核未装（503 语义）/
   二维码回调写入 / 每平台单会话冲突 / 验证码注入通道 /
   超时自动回收（5 分钟窗口，本测缩短）/ 取消 / 失败兜底 / on_success 触发；
2. HTTP 端点链路：创建（含 409/400/503 分支）/ 二维码轮询（未就绪 404 →
   image/png）/ 状态轮询契约字段 / 验证码注入（非 need_input → 409）/
   DELETE 取消 / Cookie 会话 Nonce 防护 / 成功后 account_sync 触发；
3. 账号族：``GET /accounts/status``（主目录扫描 + 基础判定）/
   ``DELETE /accounts``（删除 + 审计；缺参 400；不存在 404）/
   ``/accounts/recheck`` 仍 501 占位。

运行：``.venv\\Scripts\\python.exe sau_wrap\\tests\\verify_s9.py``
数据隔离：``SAU_DATA_ROOT`` → 本目录 ``_tmpdata9``。退出码 0=全部通过。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import logging
import os
import socket
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _REPO_ROOT)
os.environ["SAU_DATA_ROOT"] = os.path.join(_HERE, "_tmpdata9")

import aiohttp                                          # noqa: E402
from sau_wrap import paths                              # noqa: E402
from sau_wrap.agent import db                          # noqa: E402
from sau_wrap.service import login_sessions as ls      # noqa: E402
from sau_wrap.service.local_api import LocalApiServer   # noqa: E402

RESULTS: list[str] = []

#: 1x1 假 PNG（data URL 形态，模拟上游 qrcode_callback 载荷）
_FAKE_PNG = b"\x89PNG\r\n\x1a\nFAKE-QRCODE-BYTES"
_FAKE_DATA_URL = "data:image/png;base64," + base64.b64encode(_FAKE_PNG).decode()


def check(name: str, cond: bool, evidence: str) -> None:
    status = "PASS" if cond else "FAIL"
    RESULTS.append(f"[{status}] {name} :: {evidence}")
    print(f"[{status}] {name} :: {evidence}", flush=True)


def make_logger() -> tuple[logging.Logger, io.StringIO]:
    buf = io.StringIO()
    logger = logging.getLogger(f"sau.verify9.{time.time_ns()}")
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
    for f in (paths.CONFIG_FILE, paths.CREDENTIAL_FILE, paths.DB_FILE,
              paths.LOCAL_TOKEN_FILE):
        with contextlib.suppress(OSError):
            os.remove(f)
    db.db_init()


# ================================================================ 假执行器


def make_fake_executor(script: list[tuple]):
    """脚本式假登录器（任务定义 ⑥：可注入执行器，不真实打开平台页面）。

    指令：("qrcode",)（发二维码）/ ("sleep", s) / ("need_code", timeout) /
    ("success",) / ("fail", msg) / ("raise", msg) / ("hang",)（长挂等取消）。
    """
    async def executor(session: ls.LoginSession) -> None:
        for step in script:
            op = step[0]
            if op == "qrcode":
                session.accept_qrcode_payload(
                    {"image_data_url": _FAKE_DATA_URL, "image_path": ""})
            elif op == "sleep":
                await asyncio.sleep(step[1])
            elif op == "need_code":
                code = await session.wait_for_code(timeout=step[1])
                session.received_code = code  # type: ignore[attr-defined]
            elif op == "success":
                session.mark_success(message="假登录成功")
            elif op == "fail":
                session.mark_failed(step[1])
            elif op == "raise":
                raise RuntimeError(step[1])
            elif op == "hang":
                await asyncio.sleep(300)
    return executor


class FakeClient:
    """最小 mock WS 客户端（仅承载 account_sync 触发记录）。"""

    def __init__(self) -> None:
        self.sync_calls = 0

    async def send_account_sync(self) -> bool:
        self.sync_calls += 1
        return True


# ================================================================ 场景 1：管理器单元语义


async def scenario_manager() -> None:
    print("\n==== 场景1：登录会话管理器单元语义（假执行器） ====", flush=True)
    logger, _ = make_logger()

    # —— a) 平台不支持（bilibili/baijiahao/未知）
    mgr = ls.LoginSessionManager(logger, executor=make_fake_executor([("success",)]),
                                 browser_check=lambda: True)
    errs = {}
    for p in ("bilibili", "baijiahao", "wechat"):
        try:
            await mgr.create(p)
            errs[p] = "未报错"
        except ls.LoginPlatformUnsupportedError as exc:
            errs[p] = str(exc)
    check("不支持平台拒绝创建（bilibili/baijiahao/未知）",
          all(("biliup" in errs["bilibili"] or "不支持" in errs["bilibili"],
               "pause" in errs["baijiahao"] or "不支持" in errs["baijiahao"],
               "未知平台" in errs["wechat"])),
          f"reasons={errs}")

    # —— b) 浏览器内核未装
    mgr2 = ls.LoginSessionManager(logger, executor=make_fake_executor([("success",)]),
                                  browser_check=lambda: False)
    try:
        await mgr2.create("douyin")
        ok, detail = False, "未报错"
    except ls.LoginBrowserMissingError as exc:
        ok, detail = "browser install" in str(exc), str(exc)
    check("浏览器内核未装 → LoginBrowserMissingError（引导 browser install）",
          ok, detail)

    # —— c) 二维码回调写入 + 成功 + on_success 触发
    synced: list[str] = []

    async def on_success(session):
        synced.append(session.platform)

    mgr3 = ls.LoginSessionManager(
        logger, executor=make_fake_executor([("qrcode",), ("success",)]),
        browser_check=lambda: True, on_success=on_success)
    s3 = await mgr3.create("douyin", "acc1")
    await s3.task
    check("二维码回调写入（data URL 解码）+ 成功态 + on_success 触发",
          s3.qrcode_bytes == _FAKE_PNG and s3.status == "success"
          and synced == ["douyin"],
          f"qrcode={s3.qrcode_bytes[:8] if s3.qrcode_bytes else None!r} "
          f"status={s3.status} synced={synced}")

    # —— d) 每平台单会话冲突
    mgr4 = ls.LoginSessionManager(logger, executor=make_fake_executor([("hang",)]),
                                  browser_check=lambda: True)
    first = await mgr4.create("kuaishou")
    try:
        await mgr4.create("kuaishou")
        conflict_ok, detail = False, "未冲突"
    except ls.LoginSessionConflictError as exc:
        conflict_ok = exc.existing_session_id == first.session_id
        detail = f"existing={exc.existing_session_id}"
    await mgr4.cancel(first.session_id)
    check("每平台单会话：活跃期再创建 → 冲突（携带既有 session_id）",
          conflict_ok, detail)
    # 终态后同平台可再创建
    second = await mgr4.create("kuaishou")
    await mgr4.cancel(second.session_id)
    check("会话终态后同平台可再创建", second.session_id != first.session_id,
          f"first={first.session_id[:8]} second={second.session_id[:8]}")

    # —— e) need_input → 验证码注入 → 成功
    mgr5 = ls.LoginSessionManager(logger, executor=make_fake_executor(
        [("qrcode",), ("need_code", 5.0), ("success",)]),
        browser_check=lambda: True)
    s5 = await mgr5.create("douyin")
    await _wait_status(s5, "need_input")
    injected = mgr5.inject_code(s5.session_id, "123456")
    await s5.task
    check("need_input → 注入验证码 → 最终成功",
          s5.status == "success" and injected,
          f"status={s5.status} injected={injected} "
          f"received={getattr(s5, 'received_code', None)!r}")
    check("注入的验证码送达执行器", getattr(s5, "received_code", None) == "123456",
          f"received={getattr(s5, 'received_code', None)!r}")

    # —— f) 超时自动回收（窗口缩短为 1.2s）
    mgr6 = ls.LoginSessionManager(logger, executor=make_fake_executor([("hang",)]),
                                  browser_check=lambda: True, timeout=1.2)
    s6 = await mgr6.create("xiaohongshu")
    await s6.task
    check("会话总超时自动置 timeout（§6.5 五分钟，本测缩短窗口）",
          s6.status == "timeout" and "超时" in s6.message,
          f"status={s6.status} message={s6.message}")

    # —— g) 取消：任务终止、状态 cancelled
    mgr7 = ls.LoginSessionManager(logger, executor=make_fake_executor([("hang",)]),
                                  browser_check=lambda: True)
    s7 = await mgr7.create("tencent")
    await mgr7.cancel(s7.session_id)
    check("取消会话：状态 cancelled 且任务结束",
          s7.status == "cancelled" and s7.task.done(),
          f"status={s7.status} task_done={s7.task.done()}")

    # —— h) 执行器异常兜底为 failed
    mgr8 = ls.LoginSessionManager(logger, executor=make_fake_executor(
        [("raise", "模拟浏览器崩溃")]), browser_check=lambda: True)
    s8 = await mgr8.create("douyin")
    await s8.task
    check("执行器异常兜底为 failed（不拖垮服务）",
          s8.status == "failed" and "模拟浏览器崩溃" in s8.message,
          f"status={s8.status} message={s8.message}")

    # —— i) 非 need_input 时注入返回 False（409 语义）
    mgr9 = ls.LoginSessionManager(logger, executor=make_fake_executor([("hang",)]),
                                  browser_check=lambda: True)
    s9 = await mgr9.create("douyin")
    ok9 = s9.inject_code("111")
    ok9m = mgr9.inject_code(s9.session_id, "111")      # Manager 级：非 need_input
    ok9x = mgr9.inject_code("no-such-session", "111")  # Manager 级：会话不存在
    await mgr9.cancel(s9.session_id)
    check("非 need_input 状态注入验证码 → False（409 语义；Manager 级含不存在会话）",
          ok9 is False and ok9m is False and ok9x is False,
          f"inject={(ok9, ok9m, ok9x)} status_at_inject=waiting")


async def _wait_status(session, want: str, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session.status == want:
            return True
        if session.is_terminal():
            return session.status == want
        await asyncio.sleep(0.05)
    return session.status == want


# ================================================================ 场景 2：HTTP 端点链路


async def scenario_http() -> None:
    print("\n==== 场景2：登录会话 HTTP 端点链路 ====", flush=True)
    fresh_env()
    logger, logbuf = make_logger()
    client = FakeClient()
    port = free_port()

    mgr = ls.LoginSessionManager(
        logger,
        executor=make_fake_executor(
            [("sleep", 0.2), ("qrcode",), ("need_code", 6.0), ("success",)]),
        browser_check=lambda: True)
    api = LocalApiServer(logger, client, port, login_manager=mgr)
    await api.start()
    base = f"http://127.0.0.1:{port}"
    headers = {"X-SAU-Local-Token": api.token}
    try:
        async with aiohttp.ClientSession() as sess:
            # —— 创建会话（令牌鉴权豁免 Nonce）
            r1 = await sess.post(f"{base}/login/douyin", headers=headers,
                                 json={"account_name": "acc-http"})
            b1 = await r1.json()
            sid = b1.get("session_id", "")
            check("POST /login/{platform} 创建会话（返回状态机契约）",
                  r1.status == 200 and sid and b1["platform"] == "douyin"
                  and b1["status"] == "waiting" and "expires_at" in b1,
                  f"status={r1.status} body={b1}")

            # —— 重复创建 → 409
            r2 = await sess.post(f"{base}/login/douyin", headers=headers, json={})
            b2 = await r2.json()
            check("每平台单会话：重复创建 → 409 + 既有 session_id",
                  r2.status == 409 and b2.get("session_id") == sid,
                  f"status={r2.status} body={b2}")

            # —— 不支持平台 → 400；未知会话 → 404
            r3 = await sess.post(f"{base}/login/bilibili", headers=headers, json={})
            r4 = await sess.get(f"{base}/login/status/no-such", headers=headers)
            check("不支持平台 → 400；未知会话 → 404",
                  r3.status == 400 and r4.status == 404,
                  f"status={(r3.status, r4.status)} body3={await r3.json()}")

            # —— 二维码：未就绪 404 → 就绪后 image/png
            rq0 = await sess.get(f"{base}/login/qrcode/{sid}", headers=headers)
            await _wait_http_status(sess, base, sid, headers, "need_input")
            rq1 = await sess.get(f"{base}/login/qrcode/{sid}", headers=headers)
            img = await rq1.read()
            check("二维码轮询：未就绪 404 → 就绪 200 image/png（内容与回调一致）",
                  rq0.status == 404 and rq1.status == 200
                  and rq1.headers.get("Content-Type") == "image/png"
                  and img == _FAKE_PNG,
                  f"status={(rq0.status, rq1.status)} len={len(img)}")

            # —— 状态轮询契约
            rs = await sess.get(f"{base}/login/status/{sid}", headers=headers)
            bs = await rs.json()
            check("GET /login/status 契约字段（need_input 态 + message 提示）",
                  rs.status == 200 and bs["status"] == "need_input"
                  and "qrcode_ready" in bs and bs["qrcode_ready"] is True,
                  f"body={bs}")

            # —— 验证码注入 → 成功 + account_sync 触发
            rc = await sess.post(f"{base}/login/{sid}/code", headers=headers,
                                 json={"code": "654321"})
            bc = await rc.json()
            await _wait_http_status(sess, base, sid, headers, "success")
            rf = await sess.get(f"{base}/login/status/{sid}", headers=headers)
            bf = await rf.json()
            check("POST /login/{sid}/code 注入 → success + account_sync 上行",
                  rc.status == 200 and bc.get("ok") is True
                  and bf["status"] == "success" and client.sync_calls == 1,
                  f"code_resp={bc} final={bf['status']} sync_calls={client.sync_calls}")

            # —— 非 need_input 再注入 → 409；空码 → 400
            rc2 = await sess.post(f"{base}/login/{sid}/code", headers=headers,
                                  json={"code": "111"})
            rc3 = await sess.post(f"{base}/login/{sid}/code", headers=headers,
                                  json={"code": "  "})
            check("验证码注入守卫：终态注入 409 / 空码 400",
                  rc2.status == 409 and rc3.status == 400,
                  f"status={(rc2.status, rc3.status)}")

            # —— 取消：新建挂起会话 → DELETE → cancelled
            mgr._executor = make_fake_executor([("hang",)])  # noqa: SLF001（测试注入）
            r5 = await sess.post(f"{base}/login/kuaishou", headers=headers, json={})
            sid2 = (await r5.json())["session_id"]
            r6 = await sess.delete(f"{base}/login/{sid2}", headers=headers)
            b6 = await r6.json()
            r7 = await sess.get(f"{base}/login/status/{sid2}", headers=headers)
            check("DELETE /login/{sid} 取消（关会话）→ cancelled",
                  r6.status == 200 and b6["status"] == "cancelled"
                  and (await r7.json())["status"] == "cancelled",
                  f"cancel={b6} after={(await r7.json())['status']}")

            # —— Cookie 会话 Nonce 防护（票据链路换会话）
            rt = await sess.post(f"{base}/ui-ticket", headers=headers)
            ticket = (await rt.json()).get("ticket", "")
            rx = await sess.get(f"{base}/ui/t/{ticket}", allow_redirects=False)
            cookie = rx.cookies.get("sau_session")
            cookie_hdr = {"Cookie": f"sau_session={cookie.value}"} if cookie else {}
            rn1 = await sess.post(f"{base}/login/tencent", headers=cookie_hdr, json={})
            rn = await sess.get(f"{base}/nonce", headers=cookie_hdr)
            nonce = (await rn.json()).get("nonce", "")
            rn2 = await sess.post(f"{base}/login/tencent",
                                  headers={**cookie_hdr, "X-Console-Nonce": nonce},
                                  json={})
            check("Cookie 会话写防护：无 Nonce 403 → 带 Nonce 200（令牌豁免已验）",
                  bool(cookie_hdr) and rn1.status == 403 and rn2.status == 200,
                  f"status={(rn1.status, rn2.status)}")
            # 清理该会话
            sid3 = (await rn2.json()).get("session_id", "")
            if sid3:
                nn = await sess.get(f"{base}/nonce", headers=cookie_hdr)
                await sess.delete(
                    f"{base}/login/{sid3}",
                    headers={**cookie_hdr,
                             "X-Console-Nonce": (await nn.json()).get("nonce", "")})

            # —— 审计
            audit = [ln for ln in logbuf.getvalue().splitlines() if "[AUDIT]" in ln]
            check("登录族审计日志（login_start/login_code/login_cancel + via=）",
                  any("op=login_start" in ln for ln in audit)
                  and any("op=login_code" in ln for ln in audit)
                  and any("op=login_cancel" in ln for ln in audit)
                  and any("via=cookie" in ln for ln in audit),
                  f"audit_lines={len(audit)}")
    finally:
        await mgr.close_all()
        await api.stop()

    # —— 内核未装的服务端响应（503 + 引导）
    logger2, _ = make_logger()
    port2 = free_port()
    mgr2 = ls.LoginSessionManager(logger2, executor=make_fake_executor([("hang",)]),
                                  browser_check=lambda: False)
    api2 = LocalApiServer(logger2, FakeClient(), port2, login_manager=mgr2)
    await api2.start()
    try:
        async with aiohttp.ClientSession() as sess:
            rb = await sess.post(f"http://127.0.0.1:{port2}/login/douyin",
                                 headers={"X-SAU-Local-Token": api2.token}, json={})
            bb = await rb.json()
            check("内核未装 → 503 + browser install 引导（任务定义 ⑤）",
                  rb.status == 503 and bb.get("error") == "browser_missing"
                  and "browser install" in bb.get("guide", ""),
                  f"status={rb.status} body={bb}")
    finally:
        await api2.stop()

    # —— 停机路径：LocalApiServer.stop() 接 close_all（收尾：消除孤儿浏览器窗口）
    logger3, _ = make_logger()
    port3 = free_port()
    mgr3 = ls.LoginSessionManager(logger3, executor=make_fake_executor([("hang",)]),
                                  browser_check=lambda: True)
    api3 = LocalApiServer(logger3, FakeClient(), port3, login_manager=mgr3)
    await api3.start()
    sid_stop = ""
    try:
        async with aiohttp.ClientSession() as sess:
            rstop = await sess.post(f"http://127.0.0.1:{port3}/login/kuaishou",
                                    headers={"X-SAU-Local-Token": api3.token}, json={})
            sid_stop = (await rstop.json()).get("session_id", "")
    finally:
        await api3.stop()   # 内部先 close_all 再停 runner
    s_stop = mgr3.get(sid_stop) if sid_stop else None
    check("停机路径：LocalApiServer.stop() 接 close_all，活跃会话被取消",
          bool(sid_stop) and s_stop is not None and s_stop.status == "cancelled",
          f"sid={sid_stop[:8] if sid_stop else 'N/A'} "
          f"status={s_stop.status if s_stop else None}")


async def _wait_http_status(sess, base, sid, headers, want, timeout=6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await sess.get(f"{base}/login/status/{sid}", headers=headers)
        body = await r.json()
        if body.get("status") == want:
            return True
        if body.get("status") in ("success", "failed", "timeout", "cancelled") \
                and body.get("status") != want:
            return False
        await asyncio.sleep(0.1)
    return False


# ================================================================ 场景 3：账号族端点


async def scenario_accounts() -> None:
    print("\n==== 场景3：账号族端点（/accounts/status、DELETE /accounts） ====", flush=True)
    logger, logbuf = make_logger()
    port = free_port()
    client = FakeClient()
    api = LocalApiServer(logger, client, port,
                         login_manager=ls.LoginSessionManager(
                             logger, executor=make_fake_executor([("hang",)]),
                             browser_check=lambda: True))
    await api.start()
    base = f"http://127.0.0.1:{port}"
    headers = {"X-SAU-Local-Token": api.token}

    # 预置主目录 cookie（合法 JSON）+ 一个非法 JSON 文件（基础判定区分）
    paths.ensure_dir(paths.COOKIES_DIR)
    good = paths.COOKIES_DIR / "douyin_httpacc.json"
    bad = paths.COOKIES_DIR / "kuaishou_broken.json"
    good.write_text(json.dumps({"cookies": []}), encoding="utf-8")
    bad.write_text("{not-json", encoding="utf-8")
    try:
        async with aiohttp.ClientSession() as sess:
            r1 = await sess.get(f"{base}/accounts/status", headers=headers)
            b1 = await r1.json()
            accs = {(a["platform_key"], a["account_name"]): a for a in b1["accounts"]}
            check("GET /accounts/status：主目录扫描 + is_valid 基础判定 + 复核说明",
                  r1.status == 200
                  and accs.get(("douyin", "httpacc"), {}).get("is_valid") is True
                  and accs.get(("kuaishou", "broken"), {}).get("is_valid") is False
                  and "recheck" in b1.get("note", ""),
                  f"status={r1.status} accounts={b1['accounts']}")

            # —— DELETE /accounts：删除主目录文件 + 审计
            r2 = await sess.request("DELETE", f"{base}/accounts", headers=headers,
                                    json={"platform": "douyin", "account": "httpacc"})
            check("DELETE /accounts 删除主目录 cookie（成功 + 文件消失 + 审计）",
                  r2.status == 200 and not good.is_file()
                  and any("op=account_delete" in ln and "result=success" in ln
                          for ln in logbuf.getvalue().splitlines()),
                  f"status={r2.status} exists={good.is_file()}")

            # —— 缺参 400 / 不存在 404
            r3 = await sess.request("DELETE", f"{base}/accounts", headers=headers,
                                    json={"platform": "douyin"})
            r4 = await sess.request("DELETE", f"{base}/accounts", headers=headers,
                                    json={"platform": "douyin", "account": "ghost"})
            check("DELETE /accounts 守卫：缺参 400 / 不存在 404",
                  r3.status == 400 and r4.status == 404,
                  f"status={(r3.status, r4.status)}")

            # —— /accounts/recheck 仍占位 501
            r5 = await sess.post(f"{base}/accounts/recheck", headers=headers)
            check("/accounts/recheck 保持 501 占位（真实浏览器复核后续）",
                  r5.status == 501, f"status={r5.status} body={await r5.json()}")
    finally:
        for f in (good, bad):
            with contextlib.suppress(OSError):
                f.unlink()
        await api.stop()


# ================================================================ main


async def main() -> None:
    await scenario_manager()
    await scenario_http()
    await scenario_accounts()

    total = len(RESULTS)
    failed = sum(1 for r in RESULTS if r.startswith("[FAIL]"))
    print(f"\n==== 总计：{total - failed}/{total} 通过 ====", flush=True)
    report = os.path.join(_HERE, "_verify_report_s9.txt")
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(RESULTS))
    print(f"报告已写入: {report}", flush=True)
    return failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
