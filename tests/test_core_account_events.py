"""
sau_agent_pkg.core._on_event 跨线程投递单测。

背景：CookieFilesPoller 每 3s 在自己的轮询线程中触发 AccountEventBus 回调，
旧实现回退 get_event_loop_policy().get_event_loop()，在 Python 3.10+ 非主线程
无已设 loop 时抛 RuntimeError 被总线静默吞掉，导致增量 account_sync 链路失效。
修复后 _on_event 回退到 run() 保存的 _main_loop 引用，经 call_soon_threadsafe 入队。
"""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

import pytest

from sau_agent_pkg import accounts
from sau_agent_pkg.core import SauAgentCore


def _make_event() -> accounts.AccountEvent:
    return accounts.AccountEvent(
        source="scanner",
        action="added",
        platform="douyin",
        account="acc1",
    )


@pytest.fixture()
def core():
    """构造 SauAgentCore（禁掉真实轮询线程），测试后取消事件订阅。"""
    with patch("sau_agent_pkg.accounts.start_account_file_watcher"):
        core = SauAgentCore(config={"max_concurrency": 1})
    yield core
    if core._account_event_unsub:
        core._account_event_unsub()


class TestOnEventCrossThread:
    def test_event_from_non_loop_thread_is_enqueued(self, core: SauAgentCore):
        """非事件循环线程 publish 事件 → 主循环消费后事件入 _account_sync_queue。"""

        async def scenario():
            loop = asyncio.get_running_loop()
            # 模拟 run() 已保存主循环引用
            core._main_loop = loop

            published = threading.Event()

            def publish_in_thread():
                # 该线程无 running loop，复现 CookieFilesPoller 轮询线程场景
                accounts._EVENT_BUS.publish(_make_event())
                published.set()

            t = threading.Thread(target=publish_in_thread)
            t.start()
            # 等待子线程完成 publish + call_soon_threadsafe 已排入主循环回调
            while not published.is_set():
                await asyncio.sleep(0.01)
            # 让 call_soon_threadsafe 投递的 put_nowait 实际执行
            await asyncio.sleep(0.05)
            t.join()
            assert core._account_sync_queue.qsize() == 1
            payload = core._account_sync_queue.get_nowait()
            assert payload["platform"] == "douyin"
            assert payload["account"] == "acc1"
            assert payload["action"] == "added"
            assert payload["source"] == "scanner"

        asyncio.run(scenario())

    def test_event_without_loop_does_not_raise(self, core: SauAgentCore):
        """run() 尚未执行（_main_loop=None）时，非循环线程 publish 不应抛异常（事件降级丢弃）。"""
        assert core._main_loop is None

        errors: list[BaseException] = []

        def publish_in_thread():
            try:
                accounts._EVENT_BUS.publish(_make_event())
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        t = threading.Thread(target=publish_in_thread)
        t.start()
        t.join(timeout=5)
        assert not t.is_alive()
        assert errors == []
        # 事件被安全丢弃，不进入队列
        assert core._account_sync_queue.qsize() == 0

    def test_event_with_closed_loop_does_not_raise(self, core: SauAgentCore):
        """主循环已关闭（进程退出阶段）时，publish 不抛异常。"""
        loop = asyncio.new_event_loop()
        core._main_loop = loop
        loop.close()

        errors: list[BaseException] = []

        def publish_in_thread():
            try:
                accounts._EVENT_BUS.publish(_make_event())
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        t = threading.Thread(target=publish_in_thread)
        t.start()
        t.join(timeout=5)
        assert errors == []
        assert core._account_sync_queue.qsize() == 0
