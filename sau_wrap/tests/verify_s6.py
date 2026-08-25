# -*- coding: utf-8 -*-
"""S6（本地 Web 控制台）验证脚本（任务 #17 第六步）。

场景：
1. 构建产物存在性（console/dist：index.html + assets；npm run build 产出）；
2. 静态托管：/ui/ → index.html（no-cache）、assets 长缓存、未知路径 404、
   路径穿越被拒（不泄漏目录外文件）；
3. dist 缺失 → 友好提示页（503 + 构建指引）；
4. 票据全链路：无令牌签发 401 → 签发（60s）→ 核销种 Cookie（HttpOnly/
   SameSite=Strict）→ 认证访问成功；二次使用失效；过期失效；
   托盘 request_ui_ticket 真实链路（§6.3 托盘→服务→浏览器）；
5. 单实例顶替：第二次交换后旧 Cookie 401；
6. 会话 30 分钟超时（参数缩短为 1s 实测）；
7. 401 引导页：浏览器式请求返回引导 HTML；API 请求返回 JSON；
8. Nonce 写操作防护：缺失 403 / 一次性消费 / 重复 409；令牌鉴权豁免（双鉴权）；
9. 机器码只读端点；
10. 双鉴权共存（托盘令牌仍可用）+ 审计含 via=。

运行：``.venv\\Scripts\\python.exe sau_wrap\\tests\\verify_s6.py``
数据隔离：``SAU_DATA_ROOT`` → 本目录 ``_tmpdata6``。退出码 0=全部通过。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import pathlib
import re
import socket
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _REPO_ROOT)
os.environ["SAU_DATA_ROOT"] = os.path.join(_HERE, "_tmpdata6")

import aiohttp                                          # noqa: E402
from sau_wrap import paths                              # noqa: E402
from sau_wrap.agent import db                           # noqa: E402
from sau_wrap.agent import ws_client as ws_mod         # noqa: E402
from sau_wrap.agent import machine as machine_mod      # noqa: E402
from sau_wrap.service.local_api import LocalApiServer  # noqa: E402
from sau_wrap.tray import app as tray_app              # noqa: E402

RESULTS: list[str] = []
FAKE_MACHINE = "cd" * 16
_CONSOLE_DIST = os.path.join(_REPO_ROOT, "sau_wrap", "console", "dist")


def check(name: str, cond: bool, evidence: str) -> None:
    status = "PASS" if cond else "FAIL"
    RESULTS.append(f"[{status}] {name} :: {evidence}")
    print(f"[{status}] {name} :: {evidence}", flush=True)


def make_logger() -> tuple[logging.Logger, io.StringIO]:
    buf = io.StringIO()
    logger = logging.getLogger(f"sau.verify6.{time.time_ns()}")
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
    for f in (paths.CONFIG_FILE, paths.CREDENTIAL_FILE, paths.LOCAL_TOKEN_FILE):
        with contextlib.suppress(OSError):
            os.remove(f)
    db.db_init()  # /status 的 active_tasks 需 local_tasks 表


async def start_api(logger, port=None, **kwargs) -> tuple[LocalApiServer, int]:
    """启动本地 API（不起 WS 主循环；/status 用初始快照即可）。"""
    stop_event, resume_event = asyncio.Event(), asyncio.Event()
    client = ws_mod.WSClient(logger, stop_event, resume_event)
    api = LocalApiServer(logger, client, port or free_port(), **kwargs)
    await api.start()
    return api, api.port


def raw_get(host: str, port: int, path: str, timeout: float = 5.0) -> tuple[int, bytes]:
    """原始 socket 发 HTTP 请求（绕过客户端规范化，模拟路径穿越攻击）。"""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               f"Connection: close\r\n\r\n")
        sock.sendall(req.encode("ascii"))
        sock.settimeout(timeout)
        chunks: list[bytes] = []
        try:
            while True:
                d = sock.recv(65536)
                if not d:
                    break
                chunks.append(d)
        except (socket.timeout, TimeoutError):
            pass  # 服务端不回包也视为拒绝（未泄漏）
    data = b"".join(chunks)
    m = re.match(rb"HTTP/1\.\d (\d{3})", data)
    return (int(m.group(1)) if m else 0), data


def session_cookie_of(resp) -> str:
    """从 Set-Cookie 提取 sau_session 值。"""
    raw = resp.headers.get("Set-Cookie", "")
    m = re.search(r"sau_session=([^;]+)", raw)
    return m.group(1) if m else ""


async def exchange_ticket(sess, base: str, ticket: str) -> aiohttp.ClientResponse:
    return await sess.get(f"{base}/ui/t/{ticket}", allow_redirects=False)


# ================================================================ 场景 1


def scenario_build_artifacts() -> None:
    print("\n==== 场景1：构建产物存在性（npm run build 产出）====", flush=True)
    index = os.path.join(_CONSOLE_DIST, "index.html")
    assets = os.path.join(_CONSOLE_DIST, "assets")
    ok_index = os.path.isfile(index)
    html = ""
    if ok_index:
        with open(index, encoding="utf-8") as f:
            html = f.read()
    asset_files = os.listdir(assets) if os.path.isdir(assets) else []
    check("dist/index.html 存在且含应用挂载点",
          ok_index and 'id="app"' in html and "<script" in html,
          f"index={index} exists={ok_index}")
    check("dist/assets 非空（JS/CSS 产物）",
          any(a.endswith((".js", ".css")) for a in asset_files),
          f"assets={asset_files[:5]}")


# ================================================================ 场景 2/3


async def scenario_static() -> None:
    print("\n==== 场景2/3：静态托管 + 路径穿越 + dist 缺失提示 ====", flush=True)
    fresh_env()
    logger, _ = make_logger()
    api, port = await start_api(logger)
    base = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as sess:
            r0 = await sess.get(f"{base}/ui", allow_redirects=False)
            check("GET /ui（无尾斜杠）→ 302 /ui/（体验项）",
                  r0.status == 302 and r0.headers.get("Location") == "/ui/",
                  f"status={r0.status} location={r0.headers.get('Location')}")

            r1 = await sess.get(f"{base}/ui/")
            body1 = await r1.text()
            check("GET /ui/ → index.html（no-cache）",
                  r1.status == 200 and 'id="app"' in body1
                  and "no-cache" in r1.headers.get("Cache-Control", ""),
                  f"status={r1.status} cache={r1.headers.get('Cache-Control')}")

            # 找一个 asset 文件验证长缓存
            asset = next((a for a in os.listdir(os.path.join(_CONSOLE_DIST, "assets"))
                          if a.endswith(".css")), None)
            r2 = await sess.get(f"{base}/ui/assets/{asset}") if asset else None
            check("静态资源长缓存（assets）",
                  r2 is not None and r2.status == 200
                  and "max-age=86400" in r2.headers.get("Cache-Control", ""),
                  f"asset={asset} status={r2.status if r2 else '-'}"
                  f" cache={r2.headers.get('Cache-Control') if r2 else '-'}")

            r3 = await sess.get(f"{base}/ui/not-exist.js")
            check("未知路径 404（hash 路由，无服务端回退）",
                  r3.status == 404, f"status={r3.status}")

            # 路径穿越：编码 .. 指向目录外真实文件（不得泄漏内容；
            # 服务端不回包/400/404 均视为拒绝）
            leak_target = "service/local_api.py"
            code, data = raw_get("127.0.0.1", port,
                                 f"/ui/%2e%2e/%2e%2e/{leak_target}")
            leaked = b"LocalApiServer" in data
            check("路径穿越被拒（编码 .. 不泄漏目录外文件）",
                  code != 200 and not leaked,
                  f"status={code} leaked={leaked}")
            code2, data2 = raw_get("127.0.0.1", port,
                                   f"/ui/../..//{leak_target}")
            check("路径穿越被拒（明文 .. 变体）",
                  code2 != 200 and b"LocalApiServer" not in data2,
                  f"status={code2}")

        # dist 缺失 → 友好提示页
        with tempfile.TemporaryDirectory() as empty:
            logger2, _ = make_logger()
            api2, port2 = await start_api(logger2, ui_dist_dir=pathlib.Path(empty))
            try:
                async with aiohttp.ClientSession() as sess:
                    r4 = await sess.get(f"http://127.0.0.1:{port2}/ui/")
                    body4 = await r4.text()
                    check("dist 缺失 → 503 友好提示页（含构建指引）",
                          r4.status == 503 and "构建产物" in body4 and "npm run build" in body4,
                          f"status={r4.status}")
            finally:
                await api2.stop()
    finally:
        await api.stop()


# ================================================================ 场景 4


async def scenario_ticket_flow() -> None:
    print("\n==== 场景4：票据全链路（签发→核销→Cookie→访问；单次/过期）====",
          flush=True)
    fresh_env()
    logger, logbuf = make_logger()
    api, port = await start_api(logger)
    base = f"http://127.0.0.1:{port}"
    headers = {"X-SAU-Local-Token": api.token}
    try:
        async with aiohttp.ClientSession() as sess:
            r0 = await sess.post(f"{base}/ui-ticket")
            r_bad = await sess.post(f"{base}/ui-ticket",
                                    headers={"X-SAU-Local-Token": "wrong"})
            check("签发票据：无令牌/错误令牌 → 401",
                  r0.status == 401 and r_bad.status == 401,
                  f"status={(r0.status, r_bad.status)}")

            r1 = await sess.post(f"{base}/ui-ticket", headers=headers)
            b1 = await r1.json()
            ticket = b1.get("ticket", "")
            check("持令牌签发票据（60s 有效）",
                  r1.status == 200 and len(ticket) >= 32
                  and b1.get("expires_seconds") == 60,
                  f"resp={b1}")

            r2 = await exchange_ticket(sess, base, ticket)
            ck_raw = r2.headers.get("Set-Cookie", "")
            sid = session_cookie_of(r2)
            check("核销票据 → 302 /ui/ + 种会话 Cookie（HttpOnly/SameSite=Strict）",
                  r2.status == 302 and r2.headers.get("Location") == "/ui/"
                  and sid and "HttpOnly" in ck_raw and "SameSite=Strict" in ck_raw,
                  f"status={r2.status} set-cookie={ck_raw[:110]}")

            r3 = await sess.get(f"{base}/status",
                                cookies={"sau_session": sid})
            check("会话 Cookie 认证访问 /status 成功（双鉴权并存）",
                  r3.status == 200, f"status={r3.status}")

            r4 = await exchange_ticket(sess, base, ticket)  # 二次使用（API 式）
            check("票据单次核销：二次使用失效（API 式仍返回具体错误码 JSON）",
                  r4.status == 401 and (await r4.json())["error"] == "ticket_invalid",
                  f"status={r4.status}")

            # 体验项：核销失败对浏览器导航返回引导 HTML（按 Accept 判断）
            r4h = await sess.get(f"{base}/ui/t/invalid-ticket",
                                 headers={"Accept": "text/html"},
                                 allow_redirects=False)
            b4h = await r4h.text()
            check("票据核销失败：浏览器式（Accept: text/html）→ 401 引导 HTML",
                  r4h.status == 401
                  and "text/html" in r4h.headers.get("Content-Type", "")
                  and "打开控制台" in b4h,
                  f"status={r4h.status} ctype={r4h.headers.get('Content-Type')}")

            # 托盘链路（纯函数级：真实托盘为独立进程可直接同步 urllib；
            # 本脚本与服务同事件循环，同步阻塞会卡服务端，故拆分验证：
            # ① 票据 URL 构造 ② 死端口回退 None ③ 令牌头换票据）
            check("托盘 build_ticket_url 构造（/ui/t/<票据>）",
                  tray_app.build_ticket_url(port, "abc") == f"{base}/ui/t/abc",
                  f"url={tray_app.build_ticket_url(port, 'abc')}")
            check("托盘回退：服务不可达时 request_ui_ticket 返回 None",
                  tray_app.request_ui_ticket(free_port(), "whatever", timeout=1.5) is None,
                  "死端口 → None（托盘回退打开 /ui/）")
            rt = await sess.post(f"{base}/ui-ticket",
                                 headers={"X-SAU-Local-Token": api.token})
            tray_ticket = (await rt.json()).get("ticket", "")
            r5 = await exchange_ticket(sess, base, tray_ticket or "invalid")
            check("托盘链路：持本地令牌换票据 → 核销 302 种会话",
                  rt.status == 200 and bool(tray_ticket) and r5.status == 302,
                  f"ticket={'已获取' if tray_ticket else '失败'} status={r5.status}")

        # 过期：ticket_ttl=1s（注意：api2 启动会重新生成自己的令牌）
        logger2, _ = make_logger()
        api2, port2 = await start_api(logger2, ticket_ttl=1.0)
        try:
            async with aiohttp.ClientSession() as sess:
                headers2 = {"X-SAU-Local-Token": api2.token}
                rb = await (await sess.post(
                    f"http://127.0.0.1:{port2}/ui-ticket", headers=headers2)).json()
                await asyncio.sleep(1.3)
                re_ = await exchange_ticket(sess, f"http://127.0.0.1:{port2}", rb["ticket"])
                check("票据过期失效（ttl 缩短为 1s 实测）",
                      re_.status == 401 and (await re_.json())["error"] == "ticket_expired",
                      f"status={re_.status}")
        finally:
            await api2.stop()
    finally:
        await api.stop()


# ================================================================ 场景 5/6


async def scenario_session_policy() -> None:
    print("\n==== 场景5/6：单实例顶替 + 会话超时 ====", flush=True)
    fresh_env()
    logger, logbuf = make_logger()
    api, port = await start_api(logger, session_timeout=1.0)  # 超时缩短实测
    base = f"http://127.0.0.1:{port}"
    headers = {"X-SAU-Local-Token": api.token}
    try:
        async with aiohttp.ClientSession() as sess:
            async def new_session() -> str:
                t = (await (await sess.post(
                    f"{base}/ui-ticket", headers=headers)).json())["ticket"]
                r = await exchange_ticket(sess, base, t)
                return session_cookie_of(r)

            sid1 = await new_session()
            sid2 = await new_session()  # 单实例顶替
            r1 = await sess.get(f"{base}/status", cookies={"sau_session": sid1})
            r2 = await sess.get(f"{base}/status", cookies={"sau_session": sid2})
            check("单实例顶替：新会话有效、旧 Cookie 401",
                  r1.status == 401 and r2.status == 200,
                  f"old={r1.status} new={r2.status}")
            check("顶替有日志记录", "顶替" in logbuf.getvalue(),
                  "日志含「新会话顶替旧会话」")

            await asyncio.sleep(1.3)  # 超过缩短后的 1s 会话超时
            r3 = await sess.get(f"{base}/status", cookies={"sau_session": sid2})
            check("会话无活动超时（30 分钟定案，1s 缩短实测）",
                  r3.status == 401, f"status={r3.status}")
    finally:
        await api.stop()


# ================================================================ 场景 7


async def scenario_401_guide() -> None:
    print("\n==== 场景7：401 引导页（浏览器式 vs API 请求）====", flush=True)
    fresh_env()
    logger, _ = make_logger()
    api, port = await start_api(logger)
    base = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as sess:
            r1 = await sess.get(f"{base}/status", headers={"Accept": "text/html"})
            b1 = await r1.text()
            check("未认证浏览器式请求 → 401 引导页（从托盘打开控制台）",
                  r1.status == 401 and "text/html" in r1.headers.get("Content-Type", "")
                  and "打开控制台" in b1,
                  f"status={r1.status} ctype={r1.headers.get('Content-Type')}")
            r2 = await sess.get(f"{base}/status",
                                headers={"Accept": "application/json"})
            b2 = await r2.json()
            check("未认证 API 请求 → 401 JSON（含引导说明）",
                  r2.status == 401 and b2.get("error") == "unauthorized"
                  and "托盘" in b2.get("guide", ""),
                  f"resp={b2}")
    finally:
        await api.stop()


# ================================================================ 场景 8


async def scenario_nonce() -> None:
    print("\n==== 场景8：写操作 Nonce 双重防护（§6.3）====", flush=True)
    fresh_env()
    logger, logbuf = make_logger()
    api, port = await start_api(logger)
    base = f"http://127.0.0.1:{port}"
    headers = {"X-SAU-Local-Token": api.token}
    try:
        async with aiohttp.ClientSession() as sess:
            t = (await (await sess.post(
                f"{base}/ui-ticket", headers=headers)).json())["ticket"]
            sid = session_cookie_of(await exchange_ticket(sess, base, t))
            ck = {"sau_session": sid}

            r1 = await sess.post(f"{base}/reload", cookies=ck)
            check("Cookie 会话写操作缺 Nonce → 403",
                  r1.status == 403 and (await r1.json())["error"] == "nonce_required",
                  f"status={r1.status}")

            n1 = (await (await sess.get(f"{base}/nonce", cookies=ck)).json())["nonce"]
            r2 = await sess.post(f"{base}/reload", cookies=ck,
                                 headers={"X-Console-Nonce": n1})
            check("携带一次性 Nonce 写操作成功", r2.status == 200,
                  f"status={r2.status}")

            r3 = await sess.post(f"{base}/reload", cookies=ck,
                                 headers={"X-Console-Nonce": n1})
            check("Nonce 重复使用 → 409（一次性消费、短窗去重）",
                  r3.status == 409
                  and (await r3.json())["error"] == "nonce_invalid_or_reused",
                  f"status={r3.status}")

            r4 = await sess.post(f"{base}/reload", headers=headers)  # 令牌鉴权
            check("令牌鉴权豁免 Nonce（托盘/CLI 双鉴权不受影响）",
                  r4.status == 200, f"status={r4.status}")

            audit = [ln for ln in logbuf.getvalue().splitlines()
                     if "[AUDIT]" in ln and "op=reload" in ln]
            check("审计行含鉴权方式（via=cookie/token）",
                  any("via=cookie" in ln for ln in audit)
                  and any("via=token" in ln for ln in audit),
                  f"audit={audit[:2]}")
    finally:
        await api.stop()


# ================================================================ 场景 9


async def scenario_machine_code() -> None:
    print("\n==== 场景9：机器码只读端点 ====", flush=True)
    fresh_env()
    logger, _ = make_logger()
    orig = machine_mod.get_machine_code
    machine_mod.get_machine_code = lambda: FAKE_MACHINE
    api, port = await start_api(logger)
    base = f"http://127.0.0.1:{port}"
    headers = {"X-SAU-Local-Token": api.token}
    try:
        async with aiohttp.ClientSession() as sess:
            r = await sess.get(f"{base}/machine-code", headers=headers)
            b = await r.json()
            check("GET /machine-code 返回 32 位机器码（绑定页展示）",
                  r.status == 200 and b.get("machine_code") == FAKE_MACHINE,
                  f"resp={b}")
            r2 = await sess.get(f"{base}/machine-code")
            check("未认证访问机器码 → 401", r2.status == 401, f"status={r2.status}")
    finally:
        machine_mod.get_machine_code = orig
        await api.stop()


# ================================================================ main


async def amain() -> int:
    scenario_build_artifacts()
    await scenario_static()
    await scenario_ticket_flow()
    await scenario_session_policy()
    await scenario_401_guide()
    await scenario_nonce()
    await scenario_machine_code()
    failed = [r for r in RESULTS if r.startswith("[FAIL]")]
    print(f"\n==== 汇总：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ====",
          flush=True)
    return 1 if failed else 0


def main() -> int:
    code = asyncio.run(amain())
    report = os.path.join(_HERE, "_verify_report_s6.txt")
    with open(report, "w", encoding="utf-8") as f:
        f.write(f"S6 验证报告 @ {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("\n".join(RESULTS) + "\n")
    print(f"报告已写入: {report}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
