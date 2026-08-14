"""
sau_agent_pkg.dispatcher._build_request 单测：
覆盖 7 平台 video/note dataclass 构造、tags 字符串兼容、submit_mode=manual 映射、缺必填字段抛错。
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from sau_agent_pkg.dispatcher import DEFAULT_BILIBILI_TID, Dispatcher
from sau_agent_pkg.upstream_adapter import PLATFORMS, get_request_class


@pytest.fixture()
def dispatcher() -> Dispatcher:
    return Dispatcher(max_concurrency=1)


def _base_task(**overrides) -> dict:
    task = {
        "task_id": "t-1",
        "platform_key": "douyin",
        "content_type": "video",
        "title": "测试标题",
        "description": "测试描述",
        "tags": ["a", "b"],
    }
    task.update(overrides)
    return task


# ---------------------------------------------------------------------------
# video 分支：7 平台全部可构造（按 PLATFORMS 注册表实际注册范围）
# ---------------------------------------------------------------------------
class TestBuildVideoRequest:
    @pytest.mark.parametrize("platform", sorted(PLATFORMS))
    def test_video_construct_all_platforms(self, dispatcher: Dispatcher, platform: str):
        req_cls = get_request_class(platform, "video")
        assert req_cls is not None, f"{platform} 应注册 video 能力"
        req = dispatcher._build_request(
            req_cls, "video", "acc1", _base_task(platform_key=platform), [Path("video.mp4")]
        )
        assert isinstance(req, req_cls)
        assert req.account_name == "acc1"
        assert req.title == "测试标题"
        assert req.tags == ["a", "b"]
        assert req.video_file == Path("video.mp4")

    def test_douyin_video_description_filled(self, dispatcher: Dispatcher):
        req_cls = get_request_class("douyin", "video")
        req = dispatcher._build_request(req_cls, "video", "acc1", _base_task(), [Path("v.mp4")])
        # douyin video dataclass 必填 description
        assert req.description == "测试描述"

    @pytest.mark.parametrize("platform", ["kuaishou", "xiaohongshu", "tencent"])
    def test_description_filled(self, dispatcher: Dispatcher, platform: str):
        req_cls = get_request_class(platform, "video")
        req = dispatcher._build_request(req_cls, "video", "acc1", _base_task(), [Path("v.mp4")])
        assert req.description == "测试描述"

    def test_bilibili_tid_and_no_headless_debug(self, dispatcher: Dispatcher):
        req_cls = get_request_class("bilibili", "video")
        req = dispatcher._build_request(req_cls, "video", "acc1", _base_task(), [Path("v.mp4")])
        # bilibili 必填 tid，兜底常量 21
        assert req.tid == DEFAULT_BILIBILI_TID == 21
        assert req.description == "测试描述"
        # BilibiliVideoUploadRequest 无 headless/debug 字段，白名单不应硬传
        field_names = {f.name for f in dataclasses.fields(req_cls)}
        assert "headless" not in field_names
        assert "debug" not in field_names

    def test_youtube_no_publish_date(self, dispatcher: Dispatcher):
        req_cls = get_request_class("youtube", "video")
        # YouTubeVideoUploadRequest 无 publish_date 字段，构造不应报缺参/多参
        req = dispatcher._build_request(req_cls, "video", "acc1", _base_task(), [Path("v.mp4")])
        field_names = {f.name for f in dataclasses.fields(req_cls)}
        assert "publish_date" not in field_names
        assert req.description == "测试描述"

    def test_baijiahao_publish_date_required(self, dispatcher: Dispatcher):
        req_cls = get_request_class("baijiahao", "video")
        req = dispatcher._build_request(req_cls, "video", "acc1", _base_task(), [Path("v.mp4")])
        # baijiahao publish_date 必填，0 表示立即发布
        assert req.publish_date == 0


# ---------------------------------------------------------------------------
# note 分支
# ---------------------------------------------------------------------------
class TestBuildNoteRequest:
    @pytest.mark.parametrize("platform", ["douyin", "kuaishou", "xiaohongshu"])
    def test_note_construct(self, dispatcher: Dispatcher, platform: str):
        req_cls = get_request_class(platform, "note")
        assert req_cls is not None
        files = [Path("a.jpg"), Path("b.png")]
        req = dispatcher._build_request(req_cls, "note", "acc1", _base_task(), files)
        assert req.image_files == files
        # note 必填：正文取 description，缺失时回退 title
        assert req.note == "测试描述"

    @pytest.mark.parametrize("platform", ["douyin", "kuaishou", "xiaohongshu"])
    def test_note_fallback_to_title(self, dispatcher: Dispatcher, platform: str):
        req_cls = get_request_class(platform, "note")
        task = _base_task(description="")
        req = dispatcher._build_request(req_cls, "note", "acc1", task, [Path("a.jpg")])
        assert req.note == "测试标题"


# ---------------------------------------------------------------------------
# tags 字符串兼容（服务端过渡期）
# ---------------------------------------------------------------------------
class TestTagsCompat:
    def test_tags_csv_string(self, dispatcher: Dispatcher):
        req_cls = get_request_class("douyin", "video")
        task = _base_task(tags="a, b ,c")
        req = dispatcher._build_request(req_cls, "video", "acc1", task, [Path("v.mp4")])
        assert req.tags == ["a", "b", "c"]

    def test_tags_json_like_string(self, dispatcher: Dispatcher):
        req_cls = get_request_class("douyin", "video")
        task = _base_task(tags='["x", "y"]')
        req = dispatcher._build_request(req_cls, "video", "acc1", task, [Path("v.mp4")])
        assert req.tags == ["x", "y"]

    def test_tags_none(self, dispatcher: Dispatcher):
        req_cls = get_request_class("douyin", "video")
        task = _base_task(tags=None)
        req = dispatcher._build_request(req_cls, "video", "acc1", task, [Path("v.mp4")])
        assert req.tags == []


# ---------------------------------------------------------------------------
# submit_mode=manual：仅 tencent 映射 is_draft
# ---------------------------------------------------------------------------
class TestSubmitModeManual:
    def test_tencent_manual_maps_is_draft(self, dispatcher: Dispatcher):
        req_cls = get_request_class("tencent", "video")
        task = _base_task(submit_mode="manual")
        req = dispatcher._build_request(req_cls, "video", "acc1", task, [Path("v.mp4")])
        assert req.is_draft is True

    def test_tencent_auto_is_draft_false(self, dispatcher: Dispatcher):
        req_cls = get_request_class("tencent", "video")
        req = dispatcher._build_request(req_cls, "video", "acc1", _base_task(), [Path("v.mp4")])
        assert req.is_draft is False

    def test_other_platform_no_is_draft_field(self, dispatcher: Dispatcher):
        req_cls = get_request_class("douyin", "video")
        task = _base_task(submit_mode="manual")
        # 无 is_draft 字段的平台不应因 manual 构造失败
        req = dispatcher._build_request(req_cls, "video", "acc1", task, [Path("v.mp4")])
        assert not hasattr(req, "is_draft")


# ---------------------------------------------------------------------------
# 缺必填字段：构造失败直接抛 TypeError（不静默吞错）
# ---------------------------------------------------------------------------
class TestMissingRequired:
    def test_video_without_files_raises(self, dispatcher: Dispatcher):
        req_cls = get_request_class("douyin", "video")
        # files 为空 → video_file=None 被过滤 → 必填字段缺失
        with pytest.raises(TypeError):
            dispatcher._build_request(req_cls, "video", "acc1", _base_task(), [])

    def test_note_without_files_raises(self, dispatcher: Dispatcher):
        req_cls = get_request_class("douyin", "note")
        with pytest.raises(TypeError):
            dispatcher._build_request(req_cls, "note", "acc1", _base_task(), [])
