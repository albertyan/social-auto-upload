# -*- coding: utf-8 -*-
"""S5（瘦托盘）模块级验证脚本（任务 #16 第五步）。

托盘是交互型 GUI（pystray 消息循环），自动化验证受限——本脚本覆盖**模块级
可测逻辑**（不启动真实托盘图标）：

1. 状态轮询四态（mock /status）：在线 / 离线（未连接+挂起）/ 401 令牌错误 /
   服务不可达；
2. StateTracker 状态翻转与气泡提示触发（进入异常提示一次、同态不重复、
   恢复再提示一次、首轮不提示）；
3. Mutex 单实例（持有时二次获取失败，释放后可再获取；会话本地命名；
   错误注入：非 183 创建失败必须报错而不得返回 None）；
4. 日志轮转配置（tray.log = 5MB × 3，§14.1）；
5. 打开控制台/日志目录的 URL/路径构造 + tooltip 构造；
6. 图标生成（Pillow 色块：在线绿/离线灰，无图片资源文件）；
7. Task #4 追加：判绿纳入 token_status（expired/suspended/unbound 归离线，
   缺失按 ok）、tooltip 凭证态文案、signal_tray_exit 优雅退出信号（随机化
   命名隔离，不碰真实托盘）。
8. Task #6 追加（评审修复）：_poll_loop 等待段不再把 threading.Event 传入
   WaitForMultipleObjects（源码静态断言 + 抽出等待逻辑跑真实线程验证不抛
   TypeError 且可被退出事件打断）；pick_notice 气泡文案区分（凭证态离线 →
   凭证文案；普通离线 → 既定措辞；恢复 → 恢复措辞）。

托盘真实启动（交互会话）手动验证步骤见 sau_wrap/README.md「S5 手动验证」。

运行（仓库根目录）：
    .venv\\Scripts\\python.exe sau_wrap\\tests\\verify_s5.py

数据隔离：SAU_DATA_ROOT 指向本目录下 _tmpdata5（不触碰 %ProgramData%\\SAU）。
退出码：0=全部通过；1=存在失败。报告写 ``_verify_report_s5.txt``（UTF-8）。
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _REPO_ROOT)
os.environ["SAU_DATA_ROOT"] = os.path.join(_HERE, "_tmpdata5")

from sau_wrap import paths                                  # noqa: E402
from sau_wrap import logutil                                # noqa: E402
from sau_wrap.tray import app as tray_app                   # noqa: E402

RESULTS: list[str] = []


def check(name: str, cond: bool, evidence: str) -> None:
    status = "PASS" if cond else "FAIL"
    RESULTS.append(f"[{status}] {name} :: {evidence}")
    print(f"[{status}] {name} :: {evidence}", flush=True)


def fresh_env() -> None:
    for f in (paths.CONFIG_FILE, paths.LOCAL_TOKEN_FILE):
        with contextlib.suppress(OSError):
            os.remove(f)


# ---------------------------------------------------------------- mock /status


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    """按 X-SAU-Local-Token 值切换响应：
    - ``tok_online``  → 200 ws_connected=True
    - ``tok_offline`` → 200 ws_connected=False
    - ``tok_susp``    → 200 ws_connected=True suspended=True
    - ``tok_tsexp``   → 200 ws_connected=True token_status=expired（Task #4）
    - ``tok_tssusp``  → 200 ws_connected=True token_status=suspended（Task #4）
    - ``tok_tsunb``   → 200 ws_connected=True token_status=unbound（Task #4）
    - ``tok_tsok``    → 200 ws_connected=True token_status=ok（Task #4）
    - 其他            → 401
    """

    def do_GET(self):  # noqa: N802
        token = self.headers.get("X-SAU-Local-Token", "")
        if token == "tok_online":
            body = {"ws_connected": True, "suspended": False,
                    "version": "2.0.0a0", "active_tasks": 2}
            self._reply(200, body)
        elif token == "tok_offline":
            body = {"ws_connected": False, "suspended": False,
                    "version": "2.0.0a0", "active_tasks": 0}
            self._reply(200, body)
        elif token == "tok_susp":
            body = {"ws_connected": True, "suspended": True,
                    "version": "2.0.0a0", "active_tasks": 0}
            self._reply(200, body)
        elif token == "tok_tsexp":
            body = {"ws_connected": True, "suspended": False,
                    "token_status": "expired",
                    "version": "2.0.0a0", "active_tasks": 1}
            self._reply(200, body)
        elif token == "tok_tssusp":
            body = {"ws_connected": True, "suspended": False,
                    "token_status": "suspended",
                    "version": "2.0.0a0", "active_tasks": 1}
            self._reply(200, body)
        elif token == "tok_tsunb":
            body = {"ws_connected": True, "suspended": False,
                    "token_status": "unbound",
                    "version": "2.0.0a0", "active_tasks": 0}
            self._reply(200, body)
        elif token == "tok_tsok":
            body = {"ws_connected": True, "suspended": False,
                    "token_status": "ok",
                    "version": "2.0.0a0", "active_tasks": 2}
            self._reply(200, body)
        else:
            self._reply(401, {"error": "unauthorized"})

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 静默
        pass


def start_mock_status() -> tuple[http.server.ThreadingHTTPServer, int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StatusHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


# ================================================================ 场景 1


def scenario_poll_states(port: int, dead_port: int) -> None:
    print("\n==== 场景1：状态轮询四态 ====", flush=True)
    st, body = tray_app.fetch_status(port, "tok_online")
    check("在线（200 + ws_connected=True）",
          st == tray_app.ST_ONLINE and body and body["active_tasks"] == 2,
          f"state={st} body.active_tasks={body.get('active_tasks') if body else '-'}")

    st, _ = tray_app.fetch_status(port, "tok_offline")
    check("离线（200 + ws_connected=False）", st == tray_app.ST_OFFLINE,
          f"state={st}")

    st, _ = tray_app.fetch_status(port, "tok_susp")
    check("挂起亦判离线（200 + suspended=True）", st == tray_app.ST_OFFLINE,
          f"state={st}")

    # Task #4：判绿收紧——凭证态（/status 的 token_status）纳入在线判定；
    # expired/suspended/unbound 归离线，缺失按 ok（兼容旧版服务响应）
    st, _ = tray_app.fetch_status(port, "tok_tsexp")
    check("token_status=expired → 判离线（凭证过期不得判绿）",
          st == tray_app.ST_OFFLINE, f"state={st}")
    st, _ = tray_app.fetch_status(port, "tok_tssusp")
    check("token_status=suspended → 判离线（凭证挂起不得判绿）",
          st == tray_app.ST_OFFLINE, f"state={st}")
    st, _ = tray_app.fetch_status(port, "tok_tsunb")
    check("token_status=unbound → 判离线（未绑定不得判绿）",
          st == tray_app.ST_OFFLINE, f"state={st}")
    st, _ = tray_app.fetch_status(port, "tok_tsok")
    check("token_status=ok → 判在线（显式 ok 不影响判绿）",
          st == tray_app.ST_ONLINE, f"state={st}")

    st, _ = tray_app.fetch_status(port, "wrong_token")
    check("401 令牌错误 → auth_error", st == tray_app.ST_AUTH, f"state={st}")

    st, _ = tray_app.fetch_status(dead_port, "tok_online", timeout=2.0)
    check("服务不可达 → unreachable", st == tray_app.ST_UNREACHABLE, f"state={st}")


# ================================================================ 场景 2


def scenario_tracker() -> None:
    print("\n==== 场景2：状态翻转与气泡提示触发 ====", flush=True)
    tr = tray_app.StateTracker()
    first = tr.apply(tray_app.ST_ONLINE)
    down = tr.apply(tray_app.ST_UNREACHABLE)
    dup = tr.apply(tray_app.ST_OFFLINE)       # 异常态间切换：不重复提示
    up = tr.apply(tray_app.ST_ONLINE)
    dup2 = tr.apply(tray_app.ST_ONLINE)       # 正常态重复：不提示
    check("首轮不提示（避免开机启动期打扰）", first is None, f"first={first!r}")
    check("进入异常 → 离线提示（§5.2 定案措辞一字一致）",
          down == "服务未运行，系统会自动恢复" == tray_app.NOTIFY_DOWN,
          f"down={down!r}")
    check("异常态间切换不重复提示", dup is None, f"dup={dup!r}")
    check("恢复在线 → 再提示一次", up == tray_app.NOTIFY_UP, f"up={up!r}")
    check("正常态重复不提示", dup2 is None, f"dup2={dup2!r}")


# ================================================================ 场景 3


def scenario_mutex() -> None:
    print("\n==== 场景3：Mutex 单实例 ====", flush=True)
    check("互斥量为会话本地命名（无 Global\\ 前缀，标准用户可创建）",
          not tray_app.MUTEX_NAME.startswith("Global\\"),
          f"MUTEX_NAME={tray_app.MUTEX_NAME!r}")
    name = f"SAUTrayMutexVerify{os.getpid()}"
    h1 = tray_app.acquire_mutex(name)
    h2 = tray_app.acquire_mutex(name)
    check("首次获取互斥量成功", h1 is not None, f"handle={h1}")
    check("已持有时二次获取失败（183 → None，单实例语义）", h2 is None,
          f"second={h2}")
    if h1 is not None:
        import win32api

        win32api.CloseHandle(h1)
    h3 = tray_app.acquire_mutex(name)
    check("释放后可再次获取", h3 is not None, f"after_release={h3}")
    if h3 is not None:
        import win32api

        win32api.CloseHandle(h3)

    # 错误注入：非 183 创建失败（模拟标准用户被拒，如 ACCESS_DENIED=5）
    # → 必须报错，绝不得误判为「已有实例」而返回 None。
    import win32event as _w32ev

    _orig_create = _w32ev.CreateMutex

    def _inject_fail(_sa, _owner, _name):
        import ctypes

        ctypes.windll.kernel32.SetLastError(5)  # ERROR_ACCESS_DENIED
        return None

    _w32ev.CreateMutex = _inject_fail
    try:
        try:
            bad = tray_app.acquire_mutex(name)
            raised = False
        except RuntimeError as exc:
            raised = True
            bad = exc
        check("非 183 创建失败 → 抛 RuntimeError（不误报已在运行）",
              raised,
              f"raised={raised} result={bad if not raised else 'RuntimeError'}")
    finally:
        _w32ev.CreateMutex = _orig_create


# ================================================================ 场景 4


def scenario_log_rotation() -> None:
    print("\n==== 场景4：日志轮转配置（§14.1：tray.log = 5MB × 3）====", flush=True)
    size, backups = logutil._ROTATION["tray.log"]  # noqa: SLF001（测试专用）
    check("logutil 轮转表：tray.log = 5MB × 3",
          size == 5 * 1024 * 1024 and backups == 3,
          f"size={size} backups={backups}")
    logger = logutil.setup_logger("sau.verify5.tray", paths.TRAY_LOG_FILE)
    handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    ok = bool(handlers) and handlers[0].maxBytes == 5 * 1024 * 1024 \
        and handlers[0].backupCount == 3
    check("setup_logger 实际句柄参数一致",
          ok,
          f"handler={handlers[0] if handlers else '无'}"
          + (f" maxBytes={handlers[0].maxBytes} backupCount={handlers[0].backupCount}"
             if handlers else ""))
    logger.info("verify_s5 日志写入测试")
    check("tray.log 已写入（隔离目录下）",
          paths.TRAY_LOG_FILE.is_file(),
          f"file={paths.TRAY_LOG_FILE} exists={paths.TRAY_LOG_FILE.is_file()}")


# ================================================================ 场景 4b（终审修复⑦）


def scenario_sanitize() -> None:
    print("\n==== 场景4b：日志脱敏（§3.8 第 3 条，终审修复⑦） ====", flush=True)
    m = logutil.mask_sensitive
    check("脱敏：凭证键值只记长度 / Bearer 整体掩码",
          m("token=abcdef123456 done") == "token=***len=12 done"
          and m("Authorization: Bearer abcdefgh12345678")
          == "Authorization: Bearer ***",
          f"sample={m('token=abcdef123456 done')!r}")
    check("脱敏：手机号中段掩码 / 签名 URL 去 query",
          m("phone 13812345678") == "phone 138****5678"
          and m("u https://cdn.x.com/f.mp4?Signature=abc&Expires=1")
          == "u https://cdn.x.com/f.mp4?<masked>",
          f"phone={m('phone 13812345678')!r}")

    # 端到端：经 setup_logger 句柄落盘的记录不含明文凭证（含 [AUDIT] 行）
    logf = paths.LOGS_DIR / "sanitize_check.log"
    try:
        logf.unlink()
    except OSError:
        pass
    lg = logutil.setup_logger("sau.verify5.sanitize", logf)
    lg.info("[AUDIT] op=bind detail=token=aaaa1111bbbb2222")
    lg.info("ws headers Authorization: Bearer SECRETSECRET123456")
    for h in lg.handlers:
        h.flush()
    content = logf.read_text(encoding="utf-8")
    check("落盘日志不含明文凭证（含 [AUDIT] 行，句柄过滤器生效）",
          "aaaa1111bbbb2222" not in content
          and "SECRETSECRET123456" not in content
          and "***len=16" in content and "Bearer ***" in content,
          "明文泄露检查 + 掩码存在检查")


# ================================================================ 场景 5


def scenario_urls_and_tooltip() -> None:
    print("\n==== 场景5：控制台/日志目录构造 + tooltip ====", flush=True)
    fresh_env()
    url = tray_app.build_console_url(5409)
    check("控制台 URL 构造（/ui/ 根；一次性令牌链路预留后续步骤）",
          url == "http://127.0.0.1:5409/ui/", f"url={url}")
    check("默认端口 5409（未绑定 config 时）", tray_app.resolve_local_port() == 5409,
          f"port={tray_app.resolve_local_port()}")
    check("日志目录 = %ProgramData%\\SAU\\logs（隔离后为 _tmpdata5\\logs）",
          str(paths.LOGS_DIR).endswith("_tmpdata5\\logs"), f"dir={paths.LOGS_DIR}")

    tip_on = tray_app.build_tooltip(tray_app.ST_ONLINE,
                                    {"version": "2.0.0a0", "ws_connected": True,
                                     "active_tasks": 3})
    tip_susp = tray_app.build_tooltip(tray_app.ST_OFFLINE,
                                      {"ws_connected": True, "suspended": True})
    tip_unreach = tray_app.build_tooltip(tray_app.ST_UNREACHABLE, None)
    check("tooltip 在线：版本+连接态+活跃任务",
          "2.0.0a0" in tip_on and "在线" in tip_on and "3" in tip_on, f"tip={tip_on!r}")
    check("tooltip 挂起态（suspended 字段口径）", "挂起" in tip_susp, f"tip={tip_susp!r}")
    check("tooltip 不可达", "不可达" in tip_unreach, f"tip={tip_unreach!r}")

    # Task #4：token_status 非 ok 时连接态文案体现凭证问题并给处理指引；
    # 字段缺失按 ok 处理（已在上方既有在线用例覆盖，兼容旧 mock）
    tip_unb = tray_app.build_tooltip(tray_app.ST_OFFLINE,
                                     {"version": "2.0.0a0", "ws_connected": True,
                                      "token_status": "unbound", "active_tasks": 0})
    tip_exp = tray_app.build_tooltip(tray_app.ST_OFFLINE,
                                     {"version": "2.0.0a0", "ws_connected": True,
                                      "token_status": "expired", "active_tasks": 0})
    tip_tss = tray_app.build_tooltip(tray_app.ST_OFFLINE,
                                     {"version": "2.0.0a0", "ws_connected": True,
                                      "token_status": "suspended", "active_tasks": 0})
    check("tooltip 未绑定：含「未绑定（请打开控制台绑定）」",
          "未绑定（请打开控制台绑定）" in tip_unb, f"tip={tip_unb!r}")
    check("tooltip 凭证过期：含「凭证已过期（请重新绑定）」",
          "凭证已过期（请重新绑定）" in tip_exp, f"tip={tip_exp!r}")
    check("tooltip 凭证挂起：含「已挂起（凭证，请打开控制台处理）」",
          "已挂起（凭证，请打开控制台处理）" in tip_tss, f"tip={tip_tss!r}")

    # local_token.bin 读取（不存在 → None；写入 → 读回）
    check("令牌文件缺失 → None", tray_app.load_local_token() is None,
          "fresh_env 后无 local_token.bin")
    paths.LOCAL_TOKEN_FILE.write_bytes(b"tok_readback\n")
    check("令牌文件读回（去空白）",
          tray_app.load_local_token() == "tok_readback",
          f"token={tray_app.load_local_token()!r}")


# ================================================================ 场景 6


def scenario_icons() -> None:
    print("\n==== 场景6：图标生成（Pillow 色块，无图片资源）====", flush=True)
    img_on = tray_app.make_icon_image(tray_app.COLOR_ONLINE)
    img_off = tray_app.make_icon_image(tray_app.COLOR_OFFLINE)
    c_on = img_on.getpixel((32, 32))
    c_off = img_off.getpixel((32, 32))
    check("在线图标：64px 且中心为绿色块",
          img_on.size == (64, 64) and c_on[:3] == tray_app.COLOR_ONLINE,
          f"size={img_on.size} center={c_on}")
    check("离线图标：中心为灰色块且与在线不同",
          c_off[:3] == tray_app.COLOR_OFFLINE and c_off[:3] != c_on[:3],
          f"center={c_off}")
    check("pick_icon_color：在线绿，其余异常态（含离线/401/不可达）灰",
          tray_app.pick_icon_color(tray_app.ST_ONLINE) == tray_app.COLOR_ONLINE
          and tray_app.pick_icon_color(tray_app.ST_OFFLINE) == tray_app.COLOR_OFFLINE
          and tray_app.pick_icon_color(tray_app.ST_AUTH) == tray_app.COLOR_OFFLINE
          and tray_app.pick_icon_color(tray_app.ST_UNREACHABLE) == tray_app.COLOR_OFFLINE,
          "online→绿；offline/auth/unreachable→灰")

    # Task #4：图标双图预缓存 + 每轮无条件重赋（自愈）。
    # run() 为交互式入口无法模块级启动，用源码级静态断言防回归：
    import inspect

    src = inspect.getsource(tray_app.run)
    check("run() 预缓存双图 {True: 绿, False: 灰}（轮询期只赋引用不重建）",
          "icon_images = {" in src
          and "make_icon_image(COLOR_ONLINE)" in src
          and "make_icon_image(COLOR_OFFLINE)" in src,
          "icon_images pre-cache present in run()")
    check("run() 每轮无条件重赋缓存位图（图标自愈；last_online 边沿分支已删除）",
          "icon.icon = icon_images[state == ST_ONLINE]" in src
          and "last_online =" not in src and "!= last_online" not in src,
          "unconditional reassignment per poll; no edge-cache branch")


# ================================================================ 场景 7（Task #4）


def scenario_signal_tray_exit() -> None:
    print("\n==== 场景7：signal_tray_exit 优雅退出信号（卸载/升级先礼后兵） ====", flush=True)
    import uuid

    import win32api
    import win32event

    tag = uuid.uuid4().hex[:8]
    ev_missing = f"SAUVerifyNoEvent{tag}"
    mtx_missing = f"SAUVerifyNoMutex{tag}"

    # 事件不存在（托盘未运行/旧版托盘）：快速返回 0，无需兜底；
    # 随机化名称做测试隔离，确保不碰真实托盘（SAUTrayExitEvent/SAUTrayMutex）
    t0 = time.monotonic()
    rc = tray_app.signal_tray_exit(timeout=1.0, event_name=ev_missing,
                                   mutex_name=mtx_missing)
    dt = time.monotonic() - t0
    check("事件不存在 → 快速返回 0（无需 taskkill 兜底）", rc == 0, f"rc={rc}")
    check("事件不存在时快速返回（远小于 timeout）", dt < 0.5, f"elapsed={dt:.3f}s")

    # 事件存在但互斥量不存在（视同托盘已退出）：SetEvent 后探活即得 0；
    # 即使首探落在 timeout 边界后，循环也会立即终止，耗时受小 timeout 控制
    ev_h = win32event.CreateEvent(None, True, False, f"SAUVerifyEvent{tag}")
    try:
        t0 = time.monotonic()
        rc = tray_app.signal_tray_exit(timeout=0.3,
                                       event_name=f"SAUVerifyEvent{tag}",
                                       mutex_name=mtx_missing,
                                       poll_interval=0.05)
        dt = time.monotonic() - t0
        check("事件存在、互斥量不存在 → 返回 0（视同托盘已退出）",
              rc == 0, f"rc={rc}")
        check("互斥量探活快速收敛（不空转满 timeout）", dt < 0.5,
              f"elapsed={dt:.3f}s")
    finally:
        win32api.CloseHandle(ev_h)

    # 超时路径：事件与互斥量都在且「托盘」永不退出（无人消费事件）→ 1；
    # 用小 timeout 控制用例耗时（超时后调用方 taskkill 兜底）
    ev_h2 = win32event.CreateEvent(None, True, False, f"SAUVerifyEvent2{tag}")
    mtx_h = tray_app.acquire_mutex(f"SAUVerifyMutex{tag}")
    try:
        t0 = time.monotonic()
        rc = tray_app.signal_tray_exit(timeout=0.4,
                                       event_name=f"SAUVerifyEvent2{tag}",
                                       mutex_name=f"SAUVerifyMutex{tag}",
                                       poll_interval=0.1)
        dt = time.monotonic() - t0
        check("托盘仍在（互斥量存活）且超时 → 返回 1（调用方 taskkill 兜底）",
              rc == 1, f"rc={rc}")
        check("超时用例耗时受 timeout 控制（0.3~2s）", 0.3 <= dt < 2.0,
              f"elapsed={dt:.3f}s")
    finally:
        win32api.CloseHandle(ev_h2)
        if mtx_h is not None:
            win32api.CloseHandle(mtx_h)


# ================================================================ 场景 8（Task #6 评审修复①③）


def scenario_pick_notice() -> None:
    print("\n==== 场景8：气泡文案区分（评审问题 3：凭证态离线不误导「自动恢复」）====", flush=True)
    # 凭证态离线（服务在运行、根因是凭证）→ 凭证文案而非「系统会自动恢复」
    for ts in ("expired", "unbound", "suspended"):
        got = tray_app.pick_notice(tray_app.ST_OFFLINE, {"token_status": ts})
        check(f"凭证态离线（token_status={ts}）→ 凭证文案",
              got == tray_app.NOTIFY_TOKEN == "凭证异常，请打开控制台处理",
              f"notice={got!r}")

    # 普通离线（未连接/挂起/缺 token_status）→ §5.2 定案措辞一字不动
    got = tray_app.pick_notice(tray_app.ST_OFFLINE, {"ws_connected": False})
    check("普通离线（连接断开）→ 既定措辞不变",
          got == tray_app.NOTIFY_DOWN == "服务未运行，系统会自动恢复",
          f"notice={got!r}")
    got = tray_app.pick_notice(tray_app.ST_UNREACHABLE, None)
    check("服务不可达 → 既定离线措辞不变（无 body 不误判凭证）",
          got == tray_app.NOTIFY_DOWN, f"notice={got!r}")
    got = tray_app.pick_notice(tray_app.ST_AUTH, None)
    check("401 令牌不匹配 → 既定离线措辞不变（非 ST_OFFLINE 不走凭证分支）",
          got == tray_app.NOTIFY_DOWN, f"notice={got!r}")

    # 恢复在线 → 定案恢复措辞一字不动；token_status 显式 ok 不误走凭证分支
    got = tray_app.pick_notice(tray_app.ST_ONLINE, {"token_status": "ok"})
    check("恢复在线 → 「服务已恢复在线」措辞不变",
          got == tray_app.NOTIFY_UP == "服务已恢复在线", f"notice={got!r}")


# ================================================================ 场景 9（Task #6 评审修复①）


def scenario_poll_wait_section() -> None:
    print("\n==== 场景9：轮询等待段不再崩溃（评审问题 1）====", flush=True)
    import dis
    import inspect
    import textwrap
    import uuid

    import win32api
    import win32event

    src = inspect.getsource(tray_app.run)
    poll_src = src[src.index("def _poll_loop"):src.index("def _quit")]

    # 源码/字节码静态断言（风格与既有源码断言一致）：
    # 先抽出 _poll_loop 在桩环境编译为真实函数对象，再扫其字节码——
    # 只认实际执行的指令，注释中的历史说明不干扰断言。
    stub_ns = {
        "icon_holder": {}, "exit_event_handle": None,
        "POLL_INTERVAL": 0.1, "port": 5409,
        "load_local_token": lambda: None,
        "fetch_status": lambda port, token: (tray_app.ST_UNREACHABLE, None),
        "tracker": tray_app.StateTracker(),
        "_stop_icon": lambda reason: None,
        "logger": __import__("logging").getLogger("sau.verify5.wait.static"),
    }
    exec(compile(textwrap.dedent(poll_src), "<poll_loop>", "exec"), stub_ns)  # noqa: S102
    code_ops = [instr.argval for instr in dis.get_instructions(stub_ns["_poll_loop"])]
    has_wfm = any("WaitForMultipleObjects" in str(op) for op in code_ops)
    has_wfs = any("WaitForSingleObject" in str(op) for op in code_ops)
    check("_poll_loop 字节码不存在 WaitForMultipleObjects（threading.Event 入句柄列表必抛 TypeError）",
          not has_wfm, "dis-scanned poll-loop bytecode")
    check("等待段包含退出事件非阻塞探测 WaitForSingleObject(exit_event_handle, 0)",
          has_wfs and "WaitForSingleObject(exit_event_handle, 0)" in poll_src,
          "non-blocking probe present")
    check("等待段降级路径保留 stop_event.wait(POLL_INTERVAL)（可被菜单退出打断）",
          "stop_event.wait(POLL_INTERVAL)" in poll_src, "fallback wait present")
    # 探测段被 try/except 包裹：try 在探测前、except 在探测后（降级纯等待）
    i_try = poll_src.rindex("try:", 0, poll_src.index("WaitForSingleObject"))
    i_probe = poll_src.index("WaitForSingleObject(exit_event_handle, 0)")
    i_except = poll_src.index("except Exception", i_probe)
    check("等待段整体纳入 try/except（异常降级，轮询线程永不因等待而死）",
          i_try < i_probe < i_except, f"try@{i_try} probe@{i_probe} except@{i_except}")
    # 修复 2（探测前移）：退出事件探测必须出现在工作段（load_local_token /
    # fetch_status）之前——旧位置在工作段之后时最坏延迟 ≈5s+3s(HTTP)=8s，
    # 与 tray-exit 默认 8s 超时贴边；前移后最坏延迟 ≤1 个 POLL_INTERVAL。
    i_work = poll_src.index("load_local_token()")
    check("修复 2：退出事件探测前移至工作段之前（最坏延迟 ≤1 个 POLL_INTERVAL）",
          i_probe < i_work, f"probe@{i_probe} work@{i_work}")

    # 线程级实运：把 _poll_loop 源码抽出在桩环境跑真实线程（不启动真实托盘）：
    # ① 未置退出事件时轮询存活不抛异常；② SetEvent 后在窗口内优雅退出。
    code = textwrap.dedent(poll_src)
    tag = uuid.uuid4().hex[:8]
    ev_name = f"SAUVerifyPollEvent{tag}"
    ev_h = win32event.CreateEvent(None, True, False, ev_name)
    import logging as _logging

    stop_event = threading.Event()
    stop_reasons: list[str] = []
    ns = {
        "icon_holder": {"stop_event": stop_event},  # 无 icon：跳过图标分支
        "exit_event_handle": ev_h,
        "POLL_INTERVAL": 0.1,
        "port": 5409,  # _poll_loop 轮询体引用的外层局部量（桩值即可）
        "load_local_token": lambda: None,
        "fetch_status": lambda port, token: (tray_app.ST_UNREACHABLE, None),
        "tracker": tray_app.StateTracker(),
        "_stop_icon": lambda reason: (stop_reasons.append(reason),
                                      icon_holder_set()),
        "logger": _logging.getLogger("sau.verify5.wait"),
    }

    def icon_holder_set() -> None:
        ns["icon_holder"]["stopped"] = True
        stop_event.set()

    try:
        exec(compile(code, "<poll_loop>", "exec"), ns)  # noqa: S102（测试内桩执行）
        poll_fn = ns["_poll_loop"]
        errors: list[BaseException] = []

        def _run() -> None:
            try:
                poll_fn()
            except BaseException as exc:  # noqa: BLE001 捕获线程内任何异常（含 TypeError）
                errors.append(exc)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        time.sleep(0.3)  # 约 3 轮等待段：旧实现首轮即抛 TypeError 杀线程
        alive_before_signal = t.is_alive() and not errors
        win32event.SetEvent(ev_h)  # 模拟卸载/升级器 tray-exit 发信号
        t.join(timeout=2.0)
        exited = not t.is_alive() and not errors
        check("未置退出事件时轮询线程存活（等待段不抛异常，旧实现首轮即死）",
              alive_before_signal, f"alive={t.is_alive()} errors={errors!r}")
        check("SetEvent 后轮询线程在窗口内优雅退出（无 TypeError）",
              exited and not errors, f"exited={exited} errors={errors!r}")
        check("退出路径走 _stop_icon（与菜单退出相同：置停 → 唤醒 → icon.stop）",
              len(stop_reasons) == 1 and "外部请求退出" in stop_reasons[0],
              f"reasons={stop_reasons!r}")
    finally:
        win32api.CloseHandle(ev_h)


# ================================================================ 主入口


def _find_dead_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]  # 绑定后立即释放 → 大概率无人监听


def scenario_encoding_guard() -> None:
    print("\n==== 场景4c：入口级编码加固（终审追加：GBK 崩溃根治） ====", flush=True)
    import io as _io

    from sau_wrap import entry as entry_mod

    raw = "中文与特殊字符: ①② \u00b2 \u2713 \u00b0 emoji \U0001f600"

    # 模拟 GBK 终端流：非 CP936 字符 encode 抛 UnicodeEncodeError（真机崩溃形态）
    fake = _io.TextIOWrapper(_io.BytesIO(), encoding="gbk")
    crashed_before = False
    try:
        fake.write(raw)
        fake.flush()
    except UnicodeEncodeError:
        crashed_before = True

    old_out = sys.stdout
    sys.stdout = fake
    try:
        entry_mod.harden_stdio_encoding()  # 加固本身不得抛异常（流已是 gbk 包裹）
        crashed_after = False
        try:
            print(raw)
            sys.stdout.flush()
        except UnicodeEncodeError:
            crashed_after = True
    finally:
        sys.stdout = old_out
    check("加固前 GBK 流对非 CP936 字符抛 UnicodeEncodeError（复现真机崩溃）",
          crashed_before, "stream encoding=gbk")
    check("加固后 中文+特殊字符（U+00B2/U+2713/U+00B0/emoji）输出不抛异常",
          crashed_before and not crashed_after,
          f"before_crash={crashed_before} after_crash={crashed_after}")

    # 哑流（服务 Session 0 无控制台 / 重定向场景）：无 reconfigure 不得新崩
    class _Dummy:
        def write(self, s):
            return len(s)

        def flush(self):
            pass

    sys.stdout = _Dummy()  # type: ignore[assignment]
    old_err = sys.stderr
    sys.stderr = None      # type: ignore[assignment]（None 流分支）
    try:
        entry_mod.harden_stdio_encoding()
        dummy_ok = True
    except Exception:  # noqa: BLE001
        dummy_ok = False
    finally:
        sys.stdout = old_out
        sys.stderr = old_err
    check("哑流/None 流（服务 Session 0 与重定向场景）：加固不抛异常",
          dummy_ok, "dummy+None stream")

    # 任务 #26：控制台流（isatty=True）必须跳过 reconfigure——
    # reconfigure 会把 WriteConsoleW 路径降级为 WriteFile，控制台句柄报 EINVAL（2026-08-26 真机崩溃）
    class _FakeConsole:
        reconfigure_called = False

        def isatty(self):
            return True

        def reconfigure(self, **kw):
            _FakeConsole.reconfigure_called = True
            raise OSError(22, "Invalid argument")  # 若被调用即复现真机崩溃形态

        def write(self, s):
            return len(s)

        def flush(self):
            pass

    fc = _FakeConsole()
    old_out2, old_err2 = sys.stdout, sys.stderr
    sys.stdout = fc  # type: ignore[assignment]
    sys.stderr = fc  # type: ignore[assignment]
    try:
        entry_mod.harden_stdio_encoding()
        console_ok = not _FakeConsole.reconfigure_called
        # 控制台流不得被 _ResilientTextWriter 包裹（保持 WriteConsoleW 语义）
        not_wrapped = sys.stdout is fc
    except Exception:  # noqa: BLE001
        console_ok = False
        not_wrapped = False
    finally:
        sys.stdout, sys.stderr = old_out2, old_err2
    check("控制台流跳过 reconfigure（WriteConsoleW 保护，EINVAL 根治）",
          console_ok, "reconfigure not called on isatty stream")
    check("控制台流不被降级包装（保持原生控制台语义）", not_wrapped,
          "sys.stdout is original console stream")

    # 任务 #26：非控制台流写入失败降级丢弃（句柄失效不得崩溃）
    class _BrokenPipe:
        def isatty(self):
            return False

        def reconfigure(self, **kw):
            pass

        def write(self, s):
            raise OSError(22, "Invalid argument")

        def flush(self):
            raise OSError(22, "Invalid argument")

    old_out3, old_err3 = sys.stdout, sys.stderr
    sys.stdout = _BrokenPipe()  # type: ignore[assignment]
    sys.stderr = _BrokenPipe()  # type: ignore[assignment]
    try:
        entry_mod.harden_stdio_encoding()
        broken_ok = True
        try:
            print("任何内容 ①② \u00b2 \u2713 \U0001f600")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            broken_ok = False
    except Exception:  # noqa: BLE001
        broken_ok = False
    finally:
        sys.stdout, sys.stderr = old_out3, old_err3
    check("失效句柄写入降级丢弃（绝不因输出崩溃）", broken_ok,
          "broken pipe write/flush swallowed")

    # 任务 #26：click 二进制探测与完整输出链路（--version/--help 零输出根因防回归）：
    # click 的 _is_binary_writer 用 write(b"") 探测流类型；若 bytes 写入被吞异常，
    # 包装器会被误判为二进制流再被包编码层，输出静默全丢。
    sink = _io.BytesIO()
    tw = _io.TextIOWrapper(sink, encoding="utf-8")
    w = entry_mod._ResilientTextWriter(tw)
    probe_ok = True
    try:
        w.write(b"")            # click _is_binary_writer 探测形态：不得抛异常
        w.write(b"raw-bytes-ok")
        w.flush()
    except Exception:  # noqa: BLE001
        probe_ok = False
    bytes_ok = probe_ok and b"raw-bytes-ok" in sink.getvalue()

    old_out4, old_err4 = sys.stdout, sys.stderr
    sys.stdout = w              # type: ignore[assignment]
    sys.stderr = w              # type: ignore[assignment]
    click_ok = False
    try:
        import click as _click
        _click.echo("探针可见输出 ①")
        click_ok = "探针可见输出 ①".encode("utf-8") in sink.getvalue()
    except Exception:  # noqa: BLE001
        click_ok = False
    finally:
        sys.stdout, sys.stderr = old_out4, old_err4
    check("弹性包装器接受 bytes 写入（click 二进制探测不误判）",
          bytes_ok, f"probe_ok={probe_ok} bytes_in_sink={bytes_ok}")
    check("click.echo 经弹性包装器全链路可见（零输出防回归）",
          click_ok, "click.echo payload present in underlying buffer")


def main() -> int:
    dead_port = _find_dead_port()
    server, port = start_mock_status()
    try:
        scenario_poll_states(port, dead_port)
        scenario_tracker()
        scenario_mutex()
        scenario_log_rotation()
        scenario_sanitize()
        scenario_encoding_guard()
        scenario_urls_and_tooltip()
        scenario_icons()
        scenario_signal_tray_exit()
        scenario_pick_notice()
        scenario_poll_wait_section()
    finally:
        server.shutdown()

    failed = [r for r in RESULTS if r.startswith("[FAIL]")]
    report = os.path.join(_HERE, "_verify_report_s5.txt")
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(RESULTS) + f"\n\n总计 {len(RESULTS)} 项，失败 {len(failed)} 项\n")
    print(f"\n总计 {len(RESULTS)} 项，失败 {len(failed)} 项；报告: {report}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
