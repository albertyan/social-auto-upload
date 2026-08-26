# -*- coding: utf-8 -*-
"""有头登录子进程本体（任务 #4 阶段二；运行于**用户桌面会话**，非服务进程）。

服务（Session 0 无桌面）经 :mod:`sau_wrap.service.headed_launcher` 的计划任务
把本模块投放到活跃桌面会话执行（``sau(.exe) login-headed ...``），承载
``headless=False`` 的真实浏览器窗口供用户扫码/人工干预（如抖音短信二验
直接在窗口输入，无需服务侧桥接）。

与 ``default_real_executor``（headless 分发）的**刻意差异**：

- ``headless=False``（有头，用户可见窗口）；
- ``qrcode_callback=None``（二维码直接呈现在浏览器窗口，服务端轮询通道不适用）；
- **不设** ``DOUYIN_COOKIE_AUTH_HEADLESS``（有桌面，上游默认有头复核天然成立）；
- **不挂** ``douyin_sms_bridge``（短信二验由用户在窗口内人工完成）。

生命周期纪律：

- 独立事件循环（``asyncio.run``；子进程默认策略即 Windows Proactor，
  天然支持子进程——服务主循环 Selector 的限制只在服务进程内）；
- 每 10 秒轮询 ``GET /login/status/{session_id}``（令牌每次现读）感知终态
  （如被取消 / 服务重启会话消失）→ 中断登录退出；自带 300 秒硬超时；
- 结束（成败均）现读 ``paths.LOCAL_TOKEN_FILE`` 令牌（参照
  ``upgrade.orchestrator.verify`` 现读纪律，覆盖服务重启令牌轮换），
  ``urllib`` POST ``/login/headed/result`` 回报终态，失败重试 3 次后仅记日志
  （服务侧另有会话总超时兜底收敛）；
- cookie 落 ``%ProgramData%\\SAU\\cookies\\{platform}_{account}.json``
  （``sanitize_fs_name`` 白名单校验，与 headless 链路同策略，§3.6）。

占位参数 ``channel`` / ``user_data_dir`` 本期不透传上游，仅记录日志。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.request

from sau_wrap import paths

#: 有头登录硬超时（秒；与服务会话总超时同口径，另有回报重试窗口在外）
HEADED_LOGIN_TIMEOUT = 300.0

#: 会话终态轮询间隔（秒；感知取消/服务重启会话消失）
STATUS_POLL_INTERVAL = 10.0

#: 结果回报重试次数（3 次后仅记日志——服务侧会话超时兜底收敛）
REPORT_RETRY = 3

#: session_id 白名单（同 headed_launcher：token_urlsafe 字符集）
_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: 会话消失视为终态（服务重启后内存会话不复存在，子进程应中断退出）
_GONE_STATUS = "session_not_found"


# ================================================================ 日志


def _setup_logger() -> logging.Logger:
    """子进程独立日志（``%ProgramData%\\SAU\\logs\\headed_login.log`` + stderr）。"""
    logger = logging.getLogger("sau.headed_login")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    try:
        log_file = paths.ensure_logs_dir() / "headed_login.log"
        h_file = logging.FileHandler(log_file, encoding="utf-8")
        h_file.setFormatter(fmt)
        logger.addHandler(h_file)
    except OSError:
        pass
    try:
        h_err = logging.StreamHandler(sys.stderr)
        h_err.setFormatter(fmt)
        logger.addHandler(h_err)
    except Exception:  # noqa: BLE001 - 无控制台/哑流：不得因输出通道崩溃
        pass
    return logger


# ================================================================ 本地 API 通讯


def _read_local_token() -> str:
    """现读本地令牌（每次调用重读文件——服务重启后令牌轮换，快照必失效）。"""
    try:
        return paths.LOCAL_TOKEN_FILE.read_bytes().decode("utf-8").strip()
    except OSError:
        return ""


def _local_api_port() -> int:
    """本地 API 端口（§4.4：config.json ``local_api_port`` 可覆盖，缺省 5409）。"""
    try:
        from sau_wrap.agent import config as agent_config  # noqa: PLC0415

        cfg = agent_config.load_config()
        if cfg is not None and getattr(cfg, "local_api_port", None):
            return int(cfg.local_api_port)
    except Exception:  # noqa: BLE001 - 配置不可读按默认端口
        pass
    return 5409


def _fetch_session_status(port: int, session_id: str) -> str | None:
    """``GET /login/status/{session_id}``（同步，跑于 to_thread）。

    返回状态字符串；会话不存在（404）返回 :data:`_GONE_STATUS`；
    网络/服务暂不可达返回 None（调用方下轮重试，不误杀登录）。
    """
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/login/status/{session_id}",
            headers={"X-SAU-Local-Token": _read_local_token()})
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return str(body.get("status") or "")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return _GONE_STATUS
        return None
    except Exception:  # noqa: BLE001
        return None


def _report_result(port: int, session_id: str, ok: bool, message: str,
                   logger: logging.Logger) -> bool:
    """``POST /login/headed/result`` 回报终态（令牌每次现读；3 次重试）。"""
    payload = json.dumps(
        {"session_id": session_id, "ok": ok, "message": message}).encode("utf-8")
    url = f"http://127.0.0.1:{port}/login/headed/result"
    for attempt in range(1, REPORT_RETRY + 1):
        try:
            req = urllib.request.Request(
                url, data=payload, method="POST",
                headers={"Content-Type": "application/json",
                         "X-SAU-Local-Token": _read_local_token()})
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
            logger.info("结果回报成功: ok=%s message=%s", ok, message)
            return True
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 409):
                # 评审修复 #7：会话不存在（404）/已终态幂等拒绝（409）属设计内确定性行为，
                # 重试无意义——记 info 直接返回，避免误导性 ERROR 与 3-5 秒无谓滞留；
                # 仅网络异常/5xx 走重试。
                logger.info("结果回报被确定性拒绝（HTTP %d），不重试: %s",
                            exc.code, exc)
                return False
            logger.warning("结果回报失败（第 %d/%d 次）: %s",
                           attempt, REPORT_RETRY, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("结果回报失败（第 %d/%d 次）: %s",
                           attempt, REPORT_RETRY, exc)
        if attempt < REPORT_RETRY:
            time.sleep(1.5)
    logger.error("结果回报最终失败：仅记日志（服务侧会话超时兜底收敛）")
    return False


# ================================================================ 平台登录分发


async def _platform_login(platform: str, account_file: str):
    """平台登录分发（复刻 ``_run_platform_login`` 结构，有头差异见模块注释）。"""
    if platform == "douyin":
        from uploader.douyin_uploader.main import douyin_setup  # noqa: PLC0415

        return await douyin_setup(account_file, handle=True, return_detail=True,
                                  qrcode_callback=None, headless=False)
    if platform == "kuaishou":
        from uploader.ks_uploader.main import ks_setup  # noqa: PLC0415

        return await ks_setup(account_file, handle=True, return_detail=True,
                              qrcode_callback=None, headless=False)
    if platform == "xiaohongshu":
        from uploader.xiaohongshu_uploader.main import xiaohongshu_setup  # noqa: PLC0415

        return await xiaohongshu_setup(account_file, handle=True,
                                       return_detail=True,
                                       qrcode_callback=None, headless=False)
    if platform == "tencent":
        from uploader.tencent_uploader.main import tencent_setup  # noqa: PLC0415

        return await tencent_setup(account_file, handle=True, return_detail=True,
                                   qrcode_callback=None, headless=False)
    raise ValueError(f"不支持的平台：{platform}")


class _ResultSink:
    """``_interpret_result`` 适配槽（子进程无 :class:`LoginSession` 实例，
    仅需 ``mark_success`` / ``mark_failed`` 两个语义面）。"""

    def __init__(self) -> None:
        self.ok = False
        self.message = ""
        self.cookie_file = None

    def mark_success(self, cookie_file=None, message: str = "登录成功") -> None:
        self.ok, self.message, self.cookie_file = True, message, cookie_file

    def mark_failed(self, message: str) -> None:
        self.ok, self.message = False, message


# ================================================================ 主流程


async def _watch_session(port: int, session_id: str, login_task: asyncio.Task,
                         logger: logging.Logger) -> None:
    """每 10 秒轮询会话状态感知终态（如被取消）→ 取消登录任务中断退出。"""
    from sau_wrap.service.login_sessions import TERMINAL_STATUSES  # noqa: PLC0415

    while True:
        await asyncio.sleep(STATUS_POLL_INTERVAL)
        status = await asyncio.to_thread(
            _fetch_session_status, port, session_id)
        if status is None:
            continue  # 服务暂不可达：下轮重试，不误杀进行中的登录
        if status in TERMINAL_STATUSES or status == _GONE_STATUS:
            logger.info("会话已终态（%s）→ 中断有头登录", status)
            login_task.cancel()
            return


async def _amain(platform: str, account_name: str, session_id: str | None,
                 logger: logging.Logger) -> int:
    """异步主体：登录 + 终态轮询并发 → 结果解释 → 回报。退出码 0/1。"""
    port = _local_api_port()
    paths.ensure_dir(paths.COOKIES_DIR)
    account_file = paths.COOKIES_DIR / f"{platform}_{account_name}.json"

    login_task = asyncio.create_task(_platform_login(platform, str(account_file)))
    watch_task: asyncio.Task | None = None
    if session_id:
        watch_task = asyncio.create_task(
            _watch_session(port, session_id, login_task, logger))

    timed_out = interrupted = False
    login_exc: Exception | None = None
    result = None
    try:
        result = await asyncio.wait_for(login_task, timeout=HEADED_LOGIN_TIMEOUT)
    except asyncio.TimeoutError:
        timed_out = True
        logger.warning("有头登录硬超时（%d 秒）", int(HEADED_LOGIN_TIMEOUT))
    except asyncio.CancelledError:
        # 终态轮询取消了登录任务（await 已取消任务以 CancelledError 呈现）
        interrupted = True
    except Exception as exc:  # noqa: BLE001
        logger.exception("平台登录执行异常")
        login_exc = exc

    from sau_wrap.service.login_sessions import _interpret_result  # noqa: PLC0415

    if timed_out:
        ok, message = False, f"有头登录超时（{int(HEADED_LOGIN_TIMEOUT)} 秒）"
    elif interrupted:
        ok, message = False, "有头登录被终止（登录会话已终态，如用户取消）"
    elif login_exc is not None:
        ok, message = False, f"登录执行异常：{login_exc}"
    else:
        sink = _ResultSink()
        _interpret_result(sink, result, account_file)  # type: ignore[arg-type]
        ok, message = sink.ok, sink.message

    if watch_task is not None:
        watch_task.cancel()
        try:
            await watch_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    logger.info("有头登录结束: ok=%s message=%s", ok, message)
    if session_id:
        _report_result(port, session_id, ok, message, logger)
    return 0 if ok else 1


def run_headed_login(platform: str, account_name: str = "default",
                     session_id: str | None = None,
                     channel: str | None = None,
                     user_data_dir: str | None = None) -> int:
    """有头登录子进程入口（``sau login-headed`` / 计划任务投放共用）。

    独立事件循环：``asyncio.run`` 走默认策略（Windows 即 Proactor），
    天然支持子进程——服务主循环 Selector 的限制仅存在于服务进程内。

    ``channel`` / ``user_data_dir`` 为占位参数：本期不透传上游，仅记录日志。
    """
    logger = _setup_logger()
    logger.info("有头登录子进程启动: platform=%s account=%s session=%s "
                "channel=%r user_data_dir=%r（后两者本期占位不透传）",
                platform, account_name, session_id, channel, user_data_dir)

    from sau_wrap.service.login_sessions import sanitize_fs_name  # noqa: PLC0415

    platform = (platform or "").strip().lower()
    account_name = (account_name or "default").strip()
    if (sanitize_fs_name(platform) is None
            or sanitize_fs_name(account_name) is None):
        logger.error("platform/account 非法（白名单：字母/数字/_/./-，1-64）")
        return 1
    if session_id and not _SAFE_SESSION_ID_RE.fullmatch(session_id):
        logger.warning("session_id 非法（%r）→ 跳过结果回报", session_id)
        session_id = None

    try:
        return asyncio.run(_amain(platform, account_name, session_id, logger))
    except KeyboardInterrupt:
        logger.info("有头登录被外部中断（Ctrl+C）")
        if session_id:
            _report_result(_local_api_port(), session_id, False,
                           "有头登录被外部中断", logger)
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("有头登录子进程异常退出")
        if session_id:
            try:
                _report_result(_local_api_port(), session_id, False,
                               f"有头登录子进程异常：{exc}", logger)
            except Exception:  # noqa: BLE001
                pass
        return 1
