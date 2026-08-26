# -*- coding: utf-8 -*-
"""上游上传器适配层（S4；铁律：只 import 上游、绝不修改上游文件）。

- ``(platform_key, content_type)`` → 上传实现映射（对照 ``sau_cli.py`` 同平台参数组装）；
- **可测试性**：``register_uploader()`` 注入假上传函数（测试用），真实路径保留
  （真实实现惰性 import 上游模块，避免验证环境拉起 playwright）；
- 统一入口 :func:`execute_upload`：dispatcher 只依赖该函数与注册表。

平台适配表（上游函数/类与方法）：

| platform_key | content_type | 上游入口 | 备注 |
| --- | --- | --- | --- |
| douyin | video | ``DouYinVideo(...).douyin_upload_video()`` | 定时走 publish_strategy |
| douyin | note | ``DouYinNote(...).douyin_upload_note()`` | |
| kuaishou | video | ``KSVideo(...).main()`` | |
| kuaishou | note | ``KSNote(...).main()`` | |
| xiaohongshu | video | ``XiaoHongShuVideo(...).main()`` | |
| xiaohongshu | note | ``XiaoHongShuNote(...).main()`` | |
| bilibili | video | ``run_biliup_command([...])`` | 默认分区 tid=21（§5.3） |
| tencent | video | ``TencentVideo(...).tencent_upload_video()`` | 唯一支持草稿（manual） |
| youtube | video | ``YouTubeVideo(...).main()`` | 可映射但**未验证** |
| baijiahao | video | ``BaiJiaHaoVideo(...).main()`` | |
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

#: 注入/内置上传函数注册表：key=(platform_key, content_type)
_REGISTRY: dict[tuple[str, str], object] = {}

#: bilibili 默认分区（现状 §5.3：_build_request 默认 tid=21）
BILIBILI_DEFAULT_TID = 21


def register_uploader(platform_key: str, content_type: str, fn) -> None:
    """注入上传实现（测试注入假函数；重复注入覆盖）。"""
    _REGISTRY[(platform_key, content_type)] = fn


def unregister_uploader(platform_key: str, content_type: str) -> None:
    _REGISTRY.pop((platform_key, content_type), None)


def supported(platform_key: str, content_type: str) -> bool:
    """该平台×内容类型是否可执行（注入项或内置映射）。"""
    return (platform_key, content_type) in _REGISTRY or (
        (platform_key, content_type) in _BUILTIN_KEYS
    )


async def execute_upload(
    platform_key: str,
    content_type: str,
    *,
    payload: dict,
    account_file: str,
    video_file: Path | None = None,
    image_files: list[Path] | None = None,
) -> str:
    """统一上传入口。返回 publish_url（上游暂不回链，恒 ""；后续步骤可扩展）。

    - 优先使用注入实现（可测试性，仍在调用方循环直接 await）；否则走内置真实
      映射（惰性 import 上游）；
    - 上游为同步阻塞实现（subprocess/biliup）时用 ``asyncio.to_thread`` 包裹；
    - **浏览器内置上传器**（patchright/playwright 异步实现）经
      :mod:`sau_wrap.service.browser_thread` 的专用 Proactor 工作循环执行：
      服务主循环为 Selector（§5.1 定案）不支持 ``create_subprocess_*``，
      而浏览器驱动启动依赖它；只替换调度壳，不改上传协程本体（bilibili
      走 to_thread 同步子进程，不经工作线程）。
    """
    fn = _REGISTRY.get((platform_key, content_type))
    if fn is not None:
        # 注入实现（测试假函数）：不经浏览器工作线程，保持原行为
        result = fn(
            payload=payload,
            account_file=account_file,
            video_file=video_file,
            image_files=image_files or [],
        )
        if inspect.isawaitable(result):
            result = await result
        return str(result or "")

    fn = _builtin_uploader(platform_key, content_type)
    if (platform_key, content_type) in _BROWSER_BUILTIN_KEYS:
        from sau_wrap.service.browser_thread import get_browser_thread  # noqa: PLC0415

        result = await get_browser_thread().run_browser_coro(
            lambda: fn(
                payload=payload,
                account_file=account_file,
                video_file=video_file,
                image_files=image_files or [],
            ))
        return str(result or "")

    result = fn(
        payload=payload,
        account_file=account_file,
        video_file=video_file,
        image_files=image_files or [],
    )
    if inspect.isawaitable(result):
        result = await result
    return str(result or "")


# ---------------------------------------------------------------- 参数组装


def _publish_date(payload: dict) -> datetime | int:
    """``scheduled_at``（毫秒）→ datetime；缺失/已过去 → 0（立即发布，§5.3）。"""
    ms = payload.get("scheduled_at")
    if not ms:
        return 0
    dt = datetime.fromtimestamp(float(ms) / 1000.0)
    if dt <= datetime.now():
        return 0
    return dt


def _tags(payload: dict) -> list[str]:
    tags = payload.get("tags") or []
    return [str(t) for t in tags]


# ---------------------------------------------------------------- 内置真实映射

#: 内置映射键集合（supported() 判定用）
_BUILTIN_KEYS = frozenset({
    ("douyin", "video"), ("douyin", "note"),
    ("kuaishou", "video"), ("kuaishou", "note"),
    ("xiaohongshu", "video"), ("xiaohongshu", "note"),
    ("bilibili", "video"),
    ("tencent", "video"),
    ("youtube", "video"),
    ("baijiahao", "video"),
})

#: 需浏览器子进程的内置上传器（异步浏览器实现：经 browser_thread 的
#: Proactor 工作循环执行；bilibili 为同步 subprocess + to_thread，不在此列）
_BROWSER_BUILTIN_KEYS = _BUILTIN_KEYS - {("bilibili", "video")}


def _builtin_uploader(platform_key: str, content_type: str):
    key = (platform_key, content_type)
    if key not in _BUILTIN_KEYS:
        raise LookupError(f"暂不支持的平台/内容类型: {platform_key}/{content_type}")
    return _BUILTIN_BUILDERS[key]


# ---- douyin

async def _douyin_video(*, payload, account_file, video_file, image_files):
    from uploader.douyin_uploader.main import (
        DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
        DOUYIN_PUBLISH_STRATEGY_SCHEDULED,
        DouYinVideo,
    )
    publish_date = _publish_date(payload)
    app = DouYinVideo(
        payload.get("title") or "",
        str(video_file),
        _tags(payload),
        publish_date,
        str(account_file),
        desc=payload.get("description") or None,
        publish_strategy=(
            DOUYIN_PUBLISH_STRATEGY_SCHEDULED
            if isinstance(publish_date, datetime)
            else DOUYIN_PUBLISH_STRATEGY_IMMEDIATE
        ),
        debug=False,
        headless=True,
    )
    await app.douyin_upload_video()
    return ""


async def _douyin_note(*, payload, account_file, video_file, image_files):
    from uploader.douyin_uploader.main import (
        DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
        DOUYIN_PUBLISH_STRATEGY_SCHEDULED,
        DouYinNote,
    )
    publish_date = _publish_date(payload)
    app = DouYinNote(
        image_paths=[str(p) for p in image_files],
        note=payload.get("description") or "",
        tags=_tags(payload),
        publish_date=publish_date,
        account_file=str(account_file),
        title=payload.get("title") or None,
        publish_strategy=(
            DOUYIN_PUBLISH_STRATEGY_SCHEDULED
            if isinstance(publish_date, datetime)
            else DOUYIN_PUBLISH_STRATEGY_IMMEDIATE
        ),
        debug=False,
        headless=True,
    )
    await app.douyin_upload_note()
    return ""


# ---- kuaishou

async def _ks_video(*, payload, account_file, video_file, image_files):
    from uploader.ks_uploader.main import (
        KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE,
        KUAISHOU_PUBLISH_STRATEGY_SCHEDULED,
        KSVideo,
    )
    publish_date = _publish_date(payload)
    app = KSVideo(
        title=payload.get("title") or "",
        file_path=str(video_file),
        desc=payload.get("description") or None,
        tags=_tags(payload),
        publish_date=publish_date,
        account_file=str(account_file),
        publish_strategy=(
            KUAISHOU_PUBLISH_STRATEGY_SCHEDULED
            if isinstance(publish_date, datetime)
            else KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE
        ),
        debug=False,
        headless=True,
    )
    await app.main()
    return ""


async def _ks_note(*, payload, account_file, video_file, image_files):
    from uploader.ks_uploader.main import (
        KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE,
        KUAISHOU_PUBLISH_STRATEGY_SCHEDULED,
        KSNote,
    )
    publish_date = _publish_date(payload)
    app = KSNote(
        image_paths=[str(p) for p in image_files],
        note=payload.get("description") or "",
        tags=_tags(payload),
        publish_date=publish_date,
        account_file=str(account_file),
        title=payload.get("title") or None,
        publish_strategy=(
            KUAISHOU_PUBLISH_STRATEGY_SCHEDULED
            if isinstance(publish_date, datetime)
            else KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE
        ),
        debug=False,
        headless=True,
    )
    await app.main()
    return ""


# ---- xiaohongshu

async def _xhs_video(*, payload, account_file, video_file, image_files):
    from uploader.xiaohongshu_uploader.main import (
        XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED,
        XiaoHongShuVideo,
    )
    publish_date = _publish_date(payload)
    app = XiaoHongShuVideo(
        title=payload.get("title") or "",
        file_path=str(video_file),
        desc=payload.get("description") or None,
        tags=_tags(payload),
        publish_date=publish_date,
        account_file=str(account_file),
        publish_strategy=(
            XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED
            if isinstance(publish_date, datetime)
            else XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE
        ),
        debug=False,
        headless=True,
    )
    await app.main()
    return ""


async def _xhs_note(*, payload, account_file, video_file, image_files):
    from uploader.xiaohongshu_uploader.main import (
        XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED,
        XiaoHongShuNote,
    )
    publish_date = _publish_date(payload)
    app = XiaoHongShuNote(
        image_paths=[str(p) for p in image_files],
        note=payload.get("description") or "",
        tags=_tags(payload),
        publish_date=publish_date,
        account_file=str(account_file),
        title=payload.get("title") or None,
        desc=payload.get("description") or None,
        publish_strategy=(
            XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED
            if isinstance(publish_date, datetime)
            else XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE
        ),
        debug=False,
        headless=True,
    )
    await app.main()
    return ""


# ---- bilibili（biliup 子进程；同步阻塞 → to_thread）

def _bilibili_video(*, payload, account_file, video_file, image_files):
    from uploader.bilibili_uploader.runtime import run_biliup_command

    tid = BILIBILI_DEFAULT_TID
    try:
        tid = int(payload.get("tid") or BILIBILI_DEFAULT_TID)
    except (TypeError, ValueError):
        pass
    args = [
        "-u", str(account_file), "upload", str(video_file),
        "--title", payload.get("title") or "",
        "--desc", payload.get("description") or "",
        "--tid", str(tid),
    ]
    tags = _tags(payload)
    if tags:
        args.extend(["--tag", ",".join(tags)])
    publish_date = _publish_date(payload)
    if isinstance(publish_date, datetime):
        args.extend(["--dtime", str(int(publish_date.timestamp()))])

    def _run():
        result = run_biliup_command(args)
        if result.returncode != 0:
            raise RuntimeError(
                (result.stderr or result.stdout or "").strip() or "Bilibili upload failed"
            )

    return asyncio.to_thread(_run)


# ---- tencent（视频号；唯一支持草稿）

async def _tencent_video(*, payload, account_file, video_file, image_files):
    from uploader.tencent_uploader.main import (
        TENCENT_PUBLISH_STRATEGY_IMMEDIATE,
        TENCENT_PUBLISH_STRATEGY_SCHEDULED,
        TencentVideo,
    )
    publish_date = _publish_date(payload)
    is_draft = (payload.get("submit_mode") == "manual")
    app = TencentVideo(
        title=payload.get("title") or "",
        file_path=str(video_file),
        tags=_tags(payload),
        publish_date=publish_date,
        account_file=str(account_file),
        is_draft=is_draft,
        desc=payload.get("description") or None,
        publish_strategy=(
            TENCENT_PUBLISH_STRATEGY_SCHEDULED
            if isinstance(publish_date, datetime)
            else TENCENT_PUBLISH_STRATEGY_IMMEDIATE
        ),
        debug=False,
        headless=True,
    )
    await app.tencent_upload_video()
    return ""


# ---- youtube（可映射但未验证）

async def _youtube_video(*, payload, account_file, video_file, image_files):
    from uploader.youtube_uploader.main import YouTubeVideo

    app = YouTubeVideo(
        payload.get("title") or "",
        str(video_file),
        _tags(payload),
        str(account_file),
        description=payload.get("description") or "",
        debug=False,
        headless=True,
    )
    await app.main()
    return ""


# ---- baijiahao

async def _baijiahao_video(*, payload, account_file, video_file, image_files):
    from uploader.baijiahao_uploader.main import BaiJiaHaoVideo

    publish_date = _publish_date(payload)
    app = BaiJiaHaoVideo(
        payload.get("title") or "",
        str(video_file),
        _tags(payload),
        publish_date if isinstance(publish_date, datetime) else datetime.now(),
        str(account_file),
    )
    await app.main()
    return ""


_BUILTIN_BUILDERS = {
    ("douyin", "video"): _douyin_video,
    ("douyin", "note"): _douyin_note,
    ("kuaishou", "video"): _ks_video,
    ("kuaishou", "note"): _ks_note,
    ("xiaohongshu", "video"): _xhs_video,
    ("xiaohongshu", "note"): _xhs_note,
    ("bilibili", "video"): _bilibili_video,
    ("tencent", "video"): _tencent_video,
    ("youtube", "video"): _youtube_video,
    ("baijiahao", "video"): _baijiahao_video,
}
