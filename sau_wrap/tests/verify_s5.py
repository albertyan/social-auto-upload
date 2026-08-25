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
6. 图标生成（Pillow 色块：在线绿/离线灰，无图片资源文件）。

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
    check("tooltip 挂起态", "挂起" in tip_susp, f"tip={tip_susp!r}")
    check("tooltip 不可达", "不可达" in tip_unreach, f"tip={tip_unreach!r}")

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


# ================================================================ 主入口


def _find_dead_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]  # 绑定后立即释放 → 大概率无人监听


def main() -> int:
    dead_port = _find_dead_port()
    server, port = start_mock_status()
    try:
        scenario_poll_states(port, dead_port)
        scenario_tracker()
        scenario_mutex()
        scenario_log_rotation()
        scenario_urls_and_tooltip()
        scenario_icons()
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
