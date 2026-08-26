# -*- coding: utf-8 -*-
"""登录扫码会话管理器（实施计划 S9；设计文档 §6.5，任务 #21）。

职责：每平台单会话的扫码登录生命周期管理（服务进程内 headless 浏览器）。

状态机（§6.5 定案，轮询式进度）::

    waiting ──┬─> success      （cookie 落主目录 → account_sync 上行）
              ├─> need_input ──(POST code 注入)──> waiting
              ├─> failed
              ├─> timeout      （会话总超时 5 分钟自动回收）
              └─> cancelled    （DELETE 取消：关浏览器释放资源）

端点族（由 ``local_api.py`` 挂载，见该文件路由）：

    POST   /login/{platform}            创建会话（浏览器内核未装 → 503 引导）
    GET    /login/qrcode/{session_id}   二维码图片（前端每 2s 轮询，刷新自动更新）
    GET    /login/status/{session_id}   状态机轮询
    POST   /login/{session_id}/code     短信验证码注入（need_input 时）
    DELETE /login/{session_id}          取消（``POST .../cancel`` 等价）

**上游适配方案**（铁律：上游文件零修改，只读引用）：

- douyin / kuaishou / xiaohongshu / tencent 的 ``*_setup`` 原生支持
  ``qrcode_callback``（二维码提取 + 失效自动刷新再回调），直接复用；
  会话二维码经回调实时写入 :class:`LoginSession`，前端轮询即可拿到刷新后新图；
- **抖音短信二验**：上游登录链路检测到短信输入框仅记日志等待手动输入
  （有头浏览器假设），服务进程 headless 下不可用——经
  :mod:`sau_wrap.service.login_adapt` 运行时内存级适配（会话期间替换
  ``_wait_for_douyin_login`` 模块属性，结束即还原）注入验证码通道；
- bilibili（biliup 需交互终端）与 baijiahao（``page.pause()`` 需人工调试器）
  暂不支持服务端扫码登录，创建会话时返回 400 + 说明（遗留清单，见 README）。

**cookies 落盘策略**（§3.6）：登录直接写主目录
``%ProgramData%\\SAU\\cookies\\{platform}_{account}.json``——包装层自有登录
链路不经上游 ``conf.BASE_DIR/cookies``；``accounts.py`` 双目录兼容扫描使旧
仓库内 cookie 仍可读（只读回退，不搬迁）。

**可注入执行器**（任务定义 ⑥）：``executor`` 参数替换真实登录执行函数
（默认 :func:`default_real_executor`），``tests/verify_s9.py`` 注入假登录器
覆盖全状态机，不真实打开平台页面；``browser_check`` 同理可注入。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import time
from pathlib import Path

from sau_wrap import paths

#: 支持服务端扫码登录的平台（上游 ``*_setup`` 带 qrcode_callback 的四家）
SUPPORTED_PLATFORMS = ("douyin", "kuaishou", "xiaohongshu", "tencent")

#: 不支持平台的原因说明（400 响应附带，引导用户）
UNSUPPORTED_REASONS = {
    "bilibili": "bilibili 登录依赖 biliup 交互式终端，服务进程暂不支持（遗留项）",
    "baijiahao": "baijiahao 登录依赖 page.pause() 人工调试器，服务进程暂不支持（遗留项）",
    "youtube": "youtube 登录链路未适配（遗留项）",
}

#: 会话总超时（秒，§6.5：5 分钟）
LOGIN_SESSION_TIMEOUT = 300.0

#: 终态（不再轮询浏览器）
TERMINAL_STATUSES = frozenset({"success", "failed", "timeout", "cancelled"})

#: 终态会话保留时长（秒）：供前端收尾轮询读到结果，超期惰性清理
_TERMINAL_RETAIN = 600.0


class LoginPlatformUnsupportedError(ValueError):
    """平台不支持服务端扫码登录（附原因说明）。"""


class LoginBrowserMissingError(RuntimeError):
    """浏览器内核未安装（引导 ``sau.exe browser install``）。"""


class LoginSessionConflictError(RuntimeError):
    """该平台已有活跃登录会话（每平台单会话，§6.5）；携带既有 session_id。"""

    def __init__(self, existing_session_id: str):
        super().__init__(existing_session_id)
        self.existing_session_id = existing_session_id


def _decode_data_url(data_url: str) -> bytes | None:
    """解码 ``data:image/png;base64,...`` → 图片字节；失败返回 None。"""
    try:
        marker = "base64,"
        idx = data_url.index(marker)
        return base64.b64decode(data_url[idx + len(marker):])
    except (ValueError, TypeError):
        return None


class LoginSession:
    """单个登录会话（内存对象，服务重启即失效——§6.5 可接受）。"""

    def __init__(self, session_id: str, platform: str, account_name: str,
                 timeout: float):
        self.session_id = session_id
        self.platform = platform
        self.account_name = account_name
        self.created_at = time.time()
        self.expires_at = self.created_at + timeout
        self.status = "waiting"
        self.message = ""
        #: 二维码（PNG 字节）与最近更新时间（上游刷新后覆盖）
        self.qrcode_bytes: bytes | None = None
        self.qrcode_updated_at = 0.0
        #: 验证码注入通道（need_input 时创建）
        self._code_future: asyncio.Future | None = None
        #: 执行任务（管理器持有引用用于取消）
        self.task: asyncio.Task | None = None
        #: 成功落盘的 cookie 文件（成功时填充）
        self.cookie_file: Path | None = None

    # ------------------------------------------------------------ 状态迁移

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def mark_success(self, cookie_file: Path | None = None,
                     message: str = "登录成功") -> None:
        if not self.is_terminal():
            self.status = "success"
            self.message = message
            self.cookie_file = cookie_file

    def mark_failed(self, message: str) -> None:
        if not self.is_terminal():
            self.status = "failed"
            self.message = message

    # ------------------------------------------------------------ 二维码

    def accept_qrcode_payload(self, payload: dict) -> None:
        """上游 ``qrcode_callback`` 回调入口（同步/异步兼容：上游
        ``_emit_qrcode_callback`` 对同步回调直接调用）。

        payload 含 ``image_data_url``（base64）与 ``image_path``（落盘副本）；
        优先解 data URL，失败回退读文件。刷新时直接覆盖（前端轮询拿新图）。
        """
        data: bytes | None = None
        data_url = payload.get("image_data_url") or ""
        if data_url:
            data = _decode_data_url(data_url)
        if data is None and payload.get("image_path"):
            try:
                data = Path(payload["image_path"]).read_bytes()
            except OSError:
                data = None
        if data:
            self.qrcode_bytes = data
            self.qrcode_updated_at = time.time()

    # ------------------------------------------------------------ 验证码通道

    async def wait_for_code(self, timeout: float) -> str:
        """进入 ``need_input`` 并等待验证码注入（执行器内调用）。

        超时抛 ``asyncio.TimeoutError``；取消时抛 ``CancelledError``。
        """
        loop = asyncio.get_running_loop()
        self._code_future = loop.create_future()
        if not self.is_terminal():
            self.status = "need_input"
            self.message = "等待短信验证码输入"
        try:
            return await asyncio.wait_for(self._code_future, timeout=timeout)
        finally:
            self._code_future = None
            if self.status == "need_input":
                self.status = "waiting"
                self.message = ""

    def inject_code(self, code: str) -> bool:
        """注入验证码；无等待中的注入请求返回 False（409 语义）。"""
        fut = self._code_future
        if fut is None or fut.done():
            return False
        fut.set_result(code)
        return True

    # ------------------------------------------------------------ 序列化

    def to_status_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "platform": self.platform,
            "account_name": self.account_name,
            "status": self.status,
            "message": self.message,
            "created_at": int(self.created_at),
            "expires_at": int(self.expires_at),
            "qrcode_ready": self.qrcode_bytes is not None,
            "qrcode_updated_at": int(self.qrcode_updated_at),
        }


class LoginSessionManager:
    """登录会话管理器（与 5409 本地 API 同事件循环；``local_api`` 挂载）。"""

    def __init__(self, logger: logging.Logger, executor=None,
                 browser_check=None, on_success=None,
                 timeout: float = LOGIN_SESSION_TIMEOUT) -> None:
        self._logger = logger
        self._executor = executor or default_real_executor
        #: 浏览器内核前置检查（任务定义 ⑤；可注入便于测试）
        self._browser_check = browser_check or _default_browser_check
        #: 登录成功回调（async）：host 接线为 account_sync 上行（§6.5）
        self._on_success = on_success
        self._timeout = timeout
        self._sessions: dict[str, LoginSession] = {}
        self._by_platform: dict[str, str] = {}  # platform → session_id（活跃）

    def set_on_success(self, callback) -> None:
        """接线登录成功后置回调（服务端统一行为：account_sync 上行，§6.5）。
        由 ``LocalApiServer`` 在挂载时无条件接管，不依赖构造参数。"""
        self._on_success = callback

    # ------------------------------------------------------------ 创建

    async def create(self, platform: str, account_name: str = "default") -> LoginSession:
        """创建会话并启动执行任务。

        异常：平台不支持 → :class:`LoginPlatformUnsupportedError`；
        内核未装 → :class:`LoginBrowserMissingError`；
        每平台单会话冲突 → :class:`LoginSessionConflictError`。
        """
        platform = (platform or "").strip().lower()
        account_name = (account_name or "default").strip() or "default"
        if platform not in SUPPORTED_PLATFORMS:
            reason = UNSUPPORTED_REASONS.get(
                platform, f"未知平台：{platform}（支持：{', '.join(SUPPORTED_PLATFORMS)}）")
            raise LoginPlatformUnsupportedError(reason)
        if not self._browser_check():
            raise LoginBrowserMissingError(
                "浏览器内核未安装，请先执行 sau.exe browser install（§8.7）")
        self._evict_expired()
        existing = self._by_platform.get(platform)
        if existing is not None:
            sess = self._sessions.get(existing)
            if sess is not None and not sess.is_terminal() and not sess.is_expired():
                raise LoginSessionConflictError(existing)
        session = LoginSession(secrets.token_urlsafe(16), platform,
                               account_name, self._timeout)
        self._sessions[session.session_id] = session
        self._by_platform[platform] = session.session_id
        session.task = asyncio.get_running_loop().create_task(self._run(session))
        self._logger.info("登录会话创建: platform=%s account=%s session=%s",
                          platform, account_name, session.session_id)
        return session

    # ------------------------------------------------------------ 查询 / 操作

    def get(self, session_id: str) -> LoginSession | None:
        self._evict_expired()
        return self._sessions.get(session_id)

    def inject_code(self, session_id: str, code: str) -> bool:
        """注入验证码；会话不存在或不在等待中返回 False（409 语义）。

        返回 bool 而非会话对象：不吞掉注入失败信号，调用方可直接据此
        区分「会话存在且已注入」与「注入未生效」（S9 收尾）。
        """
        session = self._sessions.get(session_id)
        if session is None:
            return False
        return session.inject_code(code)

    async def cancel(self, session_id: str) -> LoginSession | None:
        """取消会话：终止执行任务（上游 finally 负责关浏览器）。"""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if not session.is_terminal():
            session.status = "cancelled"
            session.message = "用户取消"
            if session.task is not None and not session.task.done():
                session.task.cancel()
                try:
                    await session.task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._logger.info("登录会话取消: session=%s platform=%s",
                          session.session_id, session.platform)
        return session

    async def close_all(self) -> None:
        """服务退出前清理全部活跃会话。"""
        for session in list(self._sessions.values()):
            if not session.is_terminal():
                await self.cancel(session.session_id)

    # ------------------------------------------------------------ 执行

    async def _run(self, session: LoginSession) -> None:
        try:
            remaining = max(1.0, session.expires_at - time.time())
            await asyncio.wait_for(self._executor(session), timeout=remaining)
            if not session.is_terminal():
                session.mark_failed("登录执行结束但未给出明确结果")
        except asyncio.TimeoutError:
            if not session.is_terminal():
                session.status = "timeout"
                session.message = f"会话超时（{int(self._timeout)} 秒未完成登录）"
        except asyncio.CancelledError:
            if not session.is_terminal():
                session.status = "cancelled"
                session.message = "用户取消"
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("登录会话异常: session=%s error=%s",
                                 session.session_id, exc, exc_info=True)
            session.mark_failed(f"登录执行异常：{exc}")
        finally:
            if session.status == "success" and self._on_success is not None:
                try:
                    await self._on_success(session)
                except Exception:  # noqa: BLE001
                    self._logger.warning("登录成功后置动作失败（不影响登录结果）",
                                         exc_info=True)
            if self._by_platform.get(session.platform) == session.session_id:
                del self._by_platform[session.platform]
            self._logger.info("登录会话结束: session=%s status=%s message=%s",
                              session.session_id, session.status, session.message)

    def _evict_expired(self) -> None:
        """惰性清理：过期的终态会话（保留 _TERMINAL_RETAIN 供收尾轮询）。"""
        now = time.time()
        for sid in [sid for sid, s in self._sessions.items()
                    if s.is_terminal() and now - s.expires_at > _TERMINAL_RETAIN]:
            self._sessions.pop(sid, None)


# ================================================================ 真实执行器


def _default_browser_check() -> bool:
    """浏览器内核存在性检查（§8.7；惰性 import 避免测试环境拖累）。"""
    from sau_wrap import browser  # noqa: PLC0415

    return browser.is_installed()


async def default_real_executor(session: LoginSession) -> None:
    """真实登录执行器：调上游 ``*_setup``（只读引用，铁律不修改上游）。

    - cookie 直接落主目录 ``%ProgramData%\\SAU\\cookies\\``（§3.6）；
    - headless=True（服务进程 Session 0 无桌面）；抖音的有头校验回环经
      ``DOUYIN_COOKIE_AUTH_HEADLESS`` 环境变量适配（上游原生支持该开关）；
    - 抖音短信二验经 :mod:`sau_wrap.service.login_adapt` 运行时桥接
      （会话期间替换模块属性，结束还原——上游文件零修改）。
    """
    import os

    # 运行时环境适配（合法，上游原生开关）：服务进程无桌面，抖音登录后的
    # cookie 复核（上游默认有头）改走无头，避免 Session 0 有头启动失败。
    os.environ.setdefault("DOUYIN_COOKIE_AUTH_HEADLESS", "true")

    paths.ensure_dir(paths.COOKIES_DIR)
    account_file = paths.COOKIES_DIR / f"{session.platform}_{session.account_name}.json"
    af = str(account_file)
    cb = session.accept_qrcode_payload
    platform = session.platform

    if platform == "douyin":
        from uploader.douyin_uploader.main import douyin_setup  # noqa: PLC0415

        from sau_wrap.service import login_adapt  # noqa: PLC0415

        async with login_adapt.douyin_sms_bridge(session):
            result = await douyin_setup(af, handle=True, return_detail=True,
                                        qrcode_callback=cb, headless=True)
    elif platform == "kuaishou":
        from uploader.ks_uploader.main import ks_setup  # noqa: PLC0415

        result = await ks_setup(af, handle=True, return_detail=True,
                                qrcode_callback=cb, headless=True)
    elif platform == "xiaohongshu":
        from uploader.xiaohongshu_uploader.main import xiaohongshu_setup  # noqa: PLC0415

        result = await xiaohongshu_setup(af, handle=True, return_detail=True,
                                         qrcode_callback=cb, headless=True)
    elif platform == "tencent":
        from uploader.tencent_uploader.main import tencent_setup  # noqa: PLC0415

        result = await tencent_setup(af, handle=True, return_detail=True,
                                     qrcode_callback=cb, headless=True)
    else:  # 管理器已把关，防御性分支
        session.mark_failed(f"不支持的平台：{platform}")
        return

    _interpret_result(session, result, account_file)


def _interpret_result(session: LoginSession, result, account_file: Path) -> None:
    """上游返回解释：``return_detail=True`` 时为结果 dict（success/status/message）。"""
    if isinstance(result, dict):
        ok = bool(result.get("success"))
        message = result.get("message") or result.get("status") or ""
    else:
        ok = bool(result)
        message = "登录成功" if ok else "登录失败"
    if ok and account_file.is_file():
        session.mark_success(account_file, message or "登录成功")
    elif ok:
        session.mark_failed("登录流程报成功，但 cookie 文件未落盘")
    else:
        session.mark_failed(message or "登录失败")
