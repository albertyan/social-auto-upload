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

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, NamedTuple

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
    account_file = resolve_account_file("baijiahao", request.account_name)
    is_ready = await baijiahao_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"Baijiahao cookie is missing or expired: {account_file}. "
            f"Run `sau baijiahao login --account {request.account_name}` first."
        )

    app = BaiJiaHaoVideo(
        title=request.title,
        file_path=str(request.video_file),
        tags=request.tags,
        publish_date=request.publish_date,
        account_file=str(account_file),
    )
    # BaiJiaHaoVideo 使用 LOCAL_CHROME_HEADLESS 默认值，如需 headless 需额外处理
    await app.main()
    return account_file


# ---------------------------------------------------------------------------
# 百家号 Check 函数（直接 import uploader 层的 cookie_auth）
# ---------------------------------------------------------------------------
async def check_baijiahao_account(account_name: str) -> bool:
    """检查百家号账号 cookie 有效性。"""
    account_file = resolve_account_file("baijiahao", account_name)
    if not account_file.exists():
        return False
    return await baijiahao_cookie_auth(str(account_file))


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
        video=(DouyinVideoUploadRequest, upload_video),
        note=(DouyinNoteUploadRequest, upload_note),
        check=check_douyin_account,
    ),
    "kuaishou": Caps(
        video=(KuaishouVideoUploadRequest, upload_kuaishou_video),
        note=(KuaishouNoteUploadRequest, upload_kuaishou_note),
        check=check_kuaishou_account,
    ),
    "xiaohongshu": Caps(
        video=(XiaohongshuVideoUploadRequest, upload_xiaohongshu_video),
        note=(XiaohongshuNoteUploadRequest, upload_xiaohongshu_note),
        check=check_xiaohongshu_account,
    ),
    "bilibili": Caps(
        video=(BilibiliVideoUploadRequest, upload_bilibili_video),
        note=None,
        check=check_bilibili_account,
    ),
    "tencent": Caps(
        video=(TencentVideoUploadRequest, upload_tencent_video),
        note=None,
        check=check_tencent_account,
    ),
    "youtube": Caps(
        video=(YouTubeVideoUploadRequest, upload_youtube_video),
        note=None,
        check=check_youtube_account,
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
    "douyin": login_douyin_account,
    "kuaishou": login_kuaishou_account,
    "xiaohongshu": login_xiaohongshu_account,
    "bilibili": login_bilibili_account,
    "tencent": login_tencent_account,
    "youtube": login_youtube_account,
    # 百家号暂无 login 函数（上游未提供标准化接口）
}


check_fns: dict[str, Callable] = {
    "douyin": check_douyin_account,
    "kuaishou": check_kuaishou_account,
    "xiaohongshu": check_xiaohongshu_account,
    "bilibili": check_bilibili_account,
    "tencent": check_tencent_account,
    "youtube": check_youtube_account,
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
