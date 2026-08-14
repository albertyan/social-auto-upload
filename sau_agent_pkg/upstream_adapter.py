"""
sau_agent_pkg.upstream_adapter
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
上游 API 收口层：集中 import sau_cli/uploader，隔离上游变更。

这是核心隔离层，所有对上游上传/登录/检查函数的依赖都集中在此处。
上游升级时只需在此文件内做适配，**修改永不反灌上游文件**。

导出：
- PLATFORMS：平台能力静态注册表（dict）
- login_fns：登录函数映射 {platform_key: login_function}
- check_fns：检查函数映射 {platform_key: check_function}
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, NamedTuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 从 sau_cli 导入 Dataclass（上游 API 面）
# ---------------------------------------------------------------------------
from sau_cli import (
    DouyinVideoUploadRequest,
    DouyinNoteUploadRequest,
    KuaishouVideoUploadRequest,
    KuaishouNoteUploadRequest,
    XiaohongshuVideoUploadRequest,
    XiaohongshuNoteUploadRequest,
    BilibiliVideoUploadRequest,
    TencentVideoUploadRequest,
    YouTubeVideoUploadRequest,
)

# ---------------------------------------------------------------------------
# 从 sau_cli 导入 Login 函数
# ---------------------------------------------------------------------------
from sau_cli import (
    login_douyin_account,
    login_kuaishou_account,
    login_xiaohongshu_account,
    login_bilibili_account,
    login_tencent_account,
    login_youtube_account,
)

# ---------------------------------------------------------------------------
# 从 sau_cli 导入 Check 函数
# ---------------------------------------------------------------------------
from sau_cli import (
    check_douyin_account,
    check_kuaishou_account,
    check_xiaohongshu_account,
    check_bilibili_account,
    check_tencent_account,
    check_youtube_account,
)

# ---------------------------------------------------------------------------
# 从 sau_cli 导入 Upload 函数
# ---------------------------------------------------------------------------
from sau_cli import (
    upload_video,           # 抖音视频
    upload_note,            # 抖音图文
    upload_kuaishou_video,  # 快手视频
    upload_kuaishou_note,   # 快手图文
    upload_xiaohongshu_video,  # 小红书视频
    upload_xiaohongshu_note,   # 小红书图文
    upload_bilibili_video,  # B 站视频
    upload_tencent_video,   # 视频号视频
    upload_youtube_video,   # YouTube 视频
)

# ---------------------------------------------------------------------------
# 百家号特殊处理（sau_cli.py 中不存在，直接从 uploader 层导入）
# ---------------------------------------------------------------------------
from uploader.baijiahao_uploader.main import (
    baijiahao_setup,
    cookie_auth as baijiahao_cookie_auth,
    BaiJiaHaoVideo,
)

# 百家号账号文件解析（复用 sau_cli 的约定）
from sau_cli import resolve_account_file


# ---------------------------------------------------------------------------
# EventLoop Policy 临时切换上下文管理器（Playwright/子进程兼容）
# ---------------------------------------------------------------------------
# 为什么需要这个：
#   sau_service/service_host.py 为了解决匿名管道竞态崩溃问题，
#   会在 Windows 下全局强制 WindowsSelectorEventLoopPolicy。
#   但 SelectorEventLoop 不支持子进程（create_subprocess_exec），
#   Playwright 启动浏览器需要子进程，因此会抛 NotImplementedError。
#   在所有调用 Playwright 的 upload/check/login 函数前，
#   临时切回 ProactorPolicy，调用完成后（含异常路径）恢复原 policy。
#   该上下文管理器是「可重入」的：若当前已是 ProactorPolicy 或
#   外层已经切换过，则不重复切换，避免 sau_tray.login_flows 中
#   已显式切换时发生嵌套覆盖。
@contextlib.contextmanager
def _proactor_policy_for_subprocess():
    """Windows 下临时切换到 ProactorEventLoopPolicy，支持子进程（Playwright）。"""
    if sys.platform != "win32":
        yield
        return
    current_policy = asyncio.get_event_loop_policy()
    # 已是 Proactor 类型：无需切换（外层已切或默认）
    is_proactor = isinstance(
        current_policy, asyncio.WindowsProactorEventLoopPolicy  # type: ignore[attr-defined]
    )
    if is_proactor:
        yield
        return
    # 非 Windows 环境或无该属性（非 Windows）：跳过
    try:
        proactor_cls = asyncio.WindowsProactorEventLoopPolicy  # type: ignore[attr-defined]
    except AttributeError:
        yield
        return
    # 切到 Proactor，退出上下文时恢复
    asyncio.set_event_loop_policy(proactor_cls())
    logger.debug(
        "_proactor_policy_for_subprocess: 临时切换为 ProactorPolicy（用于 Playwright 子进程）"
    )
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(current_policy)
        logger.debug("_proactor_policy_for_subprocess: 已恢复原 EventLoop policy")


def _wrap_async_for_proactor(fn: Callable) -> Callable:
    """把 async upload/check/login 函数包装一层：执行期间临时切到 ProactorPolicy。"""
    # 为什么需要 functools.wraps：保留原函数名/签名，日志/traceback 里不会看到包装器名
    import functools

    @functools.wraps(fn)
    async def _wrapped(*args, **kwargs):
        with _proactor_policy_for_subprocess():
            return await fn(*args, **kwargs)

    return _wrapped


# ---------------------------------------------------------------------------
# 把从 sau_cli 导入的 upload/check/login 函数统一包装一层（处理 Playwright 子进程）
# ---------------------------------------------------------------------------
# 为什么包装：sau_cli 的这些函数内部都会启动 Playwright 浏览器子进程，
# 如果当前进程用的是 SelectorEventLoopPolicy（service_host.py 的副作用），
# 就会直接 NotImplementedError。包装一次后，所有调用方（dispatcher upload、
# accounts check、tray login 等）无需各自关心 policy 切换逻辑。
# upload
_wrapped_upload_video = _wrap_async_for_proactor(upload_video)
_wrapped_upload_note = _wrap_async_for_proactor(upload_note)
_wrapped_upload_kuaishou_video = _wrap_async_for_proactor(upload_kuaishou_video)
_wrapped_upload_kuaishou_note = _wrap_async_for_proactor(upload_kuaishou_note)
_wrapped_upload_xiaohongshu_video = _wrap_async_for_proactor(upload_xiaohongshu_video)
_wrapped_upload_xiaohongshu_note = _wrap_async_for_proactor(upload_xiaohongshu_note)
_wrapped_upload_bilibili_video = _wrap_async_for_proactor(upload_bilibili_video)
_wrapped_upload_tencent_video = _wrap_async_for_proactor(upload_tencent_video)
_wrapped_upload_youtube_video = _wrap_async_for_proactor(upload_youtube_video)
# check
_wrapped_check_douyin = _wrap_async_for_proactor(check_douyin_account)
_wrapped_check_kuaishou = _wrap_async_for_proactor(check_kuaishou_account)
_wrapped_check_xiaohongshu = _wrap_async_for_proactor(check_xiaohongshu_account)
_wrapped_check_bilibili = _wrap_async_for_proactor(check_bilibili_account)
_wrapped_check_tencent = _wrap_async_for_proactor(check_tencent_account)
_wrapped_check_youtube = _wrap_async_for_proactor(check_youtube_account)
# login
_wrapped_login_douyin = _wrap_async_for_proactor(login_douyin_account)
_wrapped_login_kuaishou = _wrap_async_for_proactor(login_kuaishou_account)
_wrapped_login_xiaohongshu = _wrap_async_for_proactor(login_xiaohongshu_account)
_wrapped_login_bilibili = _wrap_async_for_proactor(login_bilibili_account)
_wrapped_login_tencent = _wrap_async_for_proactor(login_tencent_account)
_wrapped_login_youtube = _wrap_async_for_proactor(login_youtube_account)


# ---------------------------------------------------------------------------
# 百家号 Dataclass（自行定义，模仿其他平台风格）
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class BaijiahaoVideoUploadRequest:
    """百家号视频上传请求。"""
    account_name: str
    video_file: Path
    title: str
    tags: list[str]
    publish_date: datetime | int
    debug: bool = True
    headless: bool = True


# ---------------------------------------------------------------------------
# 百家号 Upload 函数（自行实现，模仿其他 upload 函数模式）
# ---------------------------------------------------------------------------
async def upload_baijiahao_video(request: BaijiahaoVideoUploadRequest) -> Path:
    """
    上传视频到百家号。

    Args:
        request: 百家号上传请求

    Returns:
        账号文件路径

    Raises:
        RuntimeError: cookie 缺失或过期
    """
    # 为什么百家号也需要切 ProactorPolicy：BaiJiaHaoVideo.main() 内部也会
    # 启动 Playwright 浏览器子进程，SelectorEventLoop 同样不支持。
    with _proactor_policy_for_subprocess():
        account_file = resolve_account_file("baijiahao", request.account_name)
        is_ready = await baijiahao_setup(str(account_file), handle=False)
        if not is_ready:
            # cookie 缺失 info：百家号 setup 返回 False 时直接抛错，
            # info 日志让运维确认是百家号 cookie 问题（不是其他平台通用问题）
            logger.info(
                "upload_baijiahao_video: baijiahao_setup not ready, cookie missing/expired account=%s file=%s",
                request.account_name, account_file,
            )
            raise RuntimeError(
                f"Baijiahao cookie is missing or expired: {account_file}. "
                f"Run `sau baijiahao login --account {request.account_name}` first."
            )

        # 开始 info：百家号作为特殊适配分支，需要独立日志区分和抖音/快手等通用路径的差异
        logger.info("upload_baijiahao_video: start account=%s title=%s", request.account_name, request.title[:40] if request.title else "")

        app = BaiJiaHaoVideo(
            title=request.title,
            file_path=str(request.video_file),
            tags=request.tags,
            publish_date=request.publish_date,
            account_file=str(account_file),
        )
        # BaiJiaHaoVideo 使用 LOCAL_CHROME_HEADLESS 默认值，如需 headless 需额外处理
        await app.main()
        # 结束 info：确认百家号上传主流程走完（成功分支）
        logger.info("upload_baijiahao_video: done account=%s", request.account_name)
        return account_file


# ---------------------------------------------------------------------------
# 百家号 Check 函数（直接 import uploader 层的 cookie_auth）
# ---------------------------------------------------------------------------
async def check_baijiahao_account(account_name: str) -> bool:
    """检查百家号账号 cookie 有效性。"""
    # 为什么百家号也需要切 ProactorPolicy：baijiahao_cookie_auth 内部启动
    # patchright 浏览器访问创作者中心，SelectorEventLoop 不支持子进程。
    with _proactor_policy_for_subprocess():
        # debug 开始：检查启动记录，排查"检查没跑起来"时确认调用链
        logger.debug("check_baijiahao_account: start account=%s", account_name)
        account_file = resolve_account_file("baijiahao", account_name)
        if not account_file.exists():
            # 文件不存在 warning：百家号 cookie 文件路径约定不符合预期，
            # 比单纯返回 False 多一层信息，便于排查文件名/路径问题
            logger.warning("check_baijiahao_account: account file not exists account=%s file=%s", account_name, account_file)
            return False
        result = await baijiahao_cookie_auth(str(account_file))
        # 返回 bool info：百家号检查结果，配合 check_validity 中的耗时日志定位问题
        logger.info("check_baijiahao_account: result=%s account=%s", result, account_name)
        return result


# ---------------------------------------------------------------------------
# 平台能力注册表
# ---------------------------------------------------------------------------
class Caps(NamedTuple):
    """平台能力描述。"""
    video: tuple | None  # (RequestClass, upload_function) or None
    note: tuple | None   # (RequestClass, upload_function) or None
    check: Callable      # check_function


# 静态注册表（编译期确定，兼容 Nuitka）
PLATFORMS: dict[str, Caps] = {
    "douyin": Caps(
        video=(DouyinVideoUploadRequest, _wrapped_upload_video),
        note=(DouyinNoteUploadRequest, _wrapped_upload_note),
        check=_wrapped_check_douyin,
    ),
    "kuaishou": Caps(
        video=(KuaishouVideoUploadRequest, _wrapped_upload_kuaishou_video),
        note=(KuaishouNoteUploadRequest, _wrapped_upload_kuaishou_note),
        check=_wrapped_check_kuaishou,
    ),
    "xiaohongshu": Caps(
        video=(XiaohongshuVideoUploadRequest, _wrapped_upload_xiaohongshu_video),
        note=(XiaohongshuNoteUploadRequest, _wrapped_upload_xiaohongshu_note),
        check=_wrapped_check_xiaohongshu,
    ),
    "bilibili": Caps(
        video=(BilibiliVideoUploadRequest, _wrapped_upload_bilibili_video),
        note=None,
        check=_wrapped_check_bilibili,
    ),
    "tencent": Caps(
        video=(TencentVideoUploadRequest, _wrapped_upload_tencent_video),
        note=None,
        check=_wrapped_check_tencent,
    ),
    "youtube": Caps(
        video=(YouTubeVideoUploadRequest, _wrapped_upload_youtube_video),
        note=None,
        check=_wrapped_check_youtube,
    ),
    "baijiahao": Caps(
        video=(BaijiahaoVideoUploadRequest, upload_baijiahao_video),
        note=None,
        check=check_baijiahao_account,
    ),
}


# ---------------------------------------------------------------------------
# 便捷映射：login_fns / check_fns
# ---------------------------------------------------------------------------
login_fns: dict[str, Callable] = {
    "douyin": _wrapped_login_douyin,
    "kuaishou": _wrapped_login_kuaishou,
    "xiaohongshu": _wrapped_login_xiaohongshu,
    "bilibili": _wrapped_login_bilibili,
    "tencent": _wrapped_login_tencent,
    "youtube": _wrapped_login_youtube,
    # 百家号暂无 login 函数（上游未提供标准化接口）
}


check_fns: dict[str, Callable] = {
    "douyin": _wrapped_check_douyin,
    "kuaishou": _wrapped_check_kuaishou,
    "xiaohongshu": _wrapped_check_xiaohongshu,
    "bilibili": _wrapped_check_bilibili,
    "tencent": _wrapped_check_tencent,
    "youtube": _wrapped_check_youtube,
    "baijiahao": check_baijiahao_account,
}


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def get_upload_fn(platform: str, content_type: str) -> Callable | None:
    """
    获取指定平台和内容的上传函数。

    Args:
        platform: 平台 key（如 "douyin"）
        content_type: "video" 或 "note"

    Returns:
        上传函数，若该平台不支持该内容类型则返回 None
    """
    caps = PLATFORMS.get(platform)
    if not caps:
        return None
    cap = getattr(caps, content_type)
    if cap is None:
        return None
    return cap[1]  # (RequestClass, upload_function) -> upload_function


def get_request_class(platform: str, content_type: str) -> type | None:
    """
    获取指定平台和内容的 Request dataclass。

    Args:
        platform: 平台 key（如 "douyin"）
        content_type: "video" 或 "note"

    Returns:
        Request dataclass 类型，若该平台不支持该内容类型则返回 None
    """
    caps = PLATFORMS.get(platform)
    if not caps:
        return None
    cap = getattr(caps, content_type)
    if cap is None:
        return None
    return cap[0]  # (RequestClass, upload_function) -> RequestClass
