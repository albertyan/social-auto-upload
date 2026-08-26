# -*- coding: utf-8 -*-
"""浏览器专用工作线程（browser_thread）验证脚本（Selector 循环子进程修复）。

**全假注入，不真实打开平台页面**：以轻量协程/``cmd /c echo`` 子进程验证
:mod:`sau_wrap.service.browser_thread` 的跨循环桥接语义，登录验证码状态机
用 ``LoginSession`` 真实对象 + 工作循环执行体（不经上游平台链路）。

场景：
1. 根因复现 + 修复证明：Selector 主循环 ``create_subprocess_exec`` 抛
   ``NotImplementedError``；工作线程内为 Proactor 循环且可跑子进程；
2. 串行排队语义：并发投递两任务 → 线程内串行执行（最大并发=1）；
3. 协程异常以普通异常传播回主循环侧；
4. 跨循环验证码注入状态机（真实 ``LoginSession``）：「注入先于等待就绪」
   拒绝 / need_input → 主循环侧注入 → 工作循环收到 / 「等待中途取消」
   状态收敛且取消后注入拒绝；
5. 超时收敛：线程内 ``wait_for`` 超时 → 协程 finally 清理、主循环不悬挂；
6. 取消传播：句柄 ``cancel()`` 与主循环侧任务取消均链式取消工作循环任务；
7. ``stop()`` drain：残余任务被取消收敛、线程结束、重复停止幂等、
   停止后拒绝新任务。

运行：``.venv\\Scripts\\python.exe sau_wrap\\tests\\verify_browser_thread.py``
退出码 0=全部通过。
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _REPO_ROOT)
os.environ["SAU_DATA_ROOT"] = os.path.join(_HERE, "_tmpdata_bt")

from sau_wrap.service.browser_thread import (  # noqa: E402
    BrowserTaskHandle,
    BrowserThread,
)
from sau_wrap.service import login_sessions as ls  # noqa: E402

RESULTS: list[str] = []


def check(name: str, cond: bool, evidence: str) -> None:
    status = "PASS" if cond else "FAIL"
    RESULTS.append(f"[{status}] {name} :: {evidence}")
    print(f"[{status}] {name} :: {evidence}", flush=True)


def make_logger() -> logging.Logger:
    buf = io.StringIO()
    logger = logging.getLogger(f"sau.verify_bt.{time.time_ns()}")
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    return logger


async def _wait_until(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


# ================================================================ 场景 1：根因复现 + 修复证明


async def scenario_loop_types(bt: BrowserThread) -> None:
    print("\n==== 场景1：Selector 主循环不支持子进程 / 工作线程 Proactor 可跑 ====",
          flush=True)
    # —— a) 主循环为 Selector（与服务形态 §5.1 定案一致），子进程不可用
    main_loop = asyncio.get_running_loop()
    main_is_selector = "Selector" in type(main_loop).__name__
    not_impl = False
    try:
        proc = await asyncio.create_subprocess_exec(
            "cmd", "/c", "echo", "selector-no", stdout=asyncio.subprocess.PIPE)
        await proc.communicate()
    except NotImplementedError:
        not_impl = True
    check("根因复现：Selector 主循环 create_subprocess_exec 抛 NotImplementedError",
          main_is_selector and not_impl,
          f"main_loop={type(main_loop).__name__} not_implemented={not_impl}")

    # —— b) 工作循环为 Proactor，且轻量子进程可跑通（修复证明）
    async def _probe():
        loop = asyncio.get_running_loop()
        proc = await asyncio.create_subprocess_exec(
            "cmd", "/c", "echo", "proactor-ok", stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        return type(loop).__name__, out.decode().strip()

    loop_name, out = await bt.run_browser_coro(_probe, timeout=15.0)
    check("修复证明：工作线程内 Proactor 循环可跑 create_subprocess_exec",
          "Proactor" in loop_name and out == "proactor-ok",
          f"worker_loop={loop_name} stdout={out!r}")


# ================================================================ 场景 2/3：串行排队 + 异常传播


async def scenario_serial_and_errors(bt: BrowserThread) -> None:
    print("\n==== 场景2/3：串行排队语义 + 异常普通传播 ====", flush=True)
    running = 0
    max_concurrent = 0
    order: list[str] = []

    def make_job(tag: str):
        async def job():
            nonlocal running, max_concurrent
            running += 1
            max_concurrent = max(max_concurrent, running)
            order.append(f"{tag}-in")
            await asyncio.sleep(0.2)
            order.append(f"{tag}-out")
            running -= 1
            return tag
        return job

    r1, r2 = await asyncio.gather(
        bt.run_browser_coro(make_job("A"), timeout=10.0),
        bt.run_browser_coro(make_job("B"), timeout=10.0),
    )
    serial = max_concurrent == 1 and {r1, r2} == {"A", "B"}
    # 串行：一进一出交替，不存在 A-in,B-in 相邻
    interleaved = any(order[i].endswith("-in") and order[i + 1].endswith("-in")
                      for i in range(len(order) - 1))
    check("并发投递两任务 → 工作线程内串行执行（最大并发=1，无交错）",
          serial and not interleaved,
          f"max_concurrent={max_concurrent} order={order}")

    async def _boom():
        raise RuntimeError("模拟浏览器协程崩溃")

    try:
        await bt.run_browser_coro(_boom, timeout=10.0)
        ok, detail = False, "未抛出"
    except RuntimeError as exc:
        ok, detail = str(exc) == "模拟浏览器协程崩溃", str(exc)
    check("工作循环内异常以普通异常传播回主循环侧（供调用方落 failed 终态）",
          ok, detail)


# ================================================================ 场景 4：跨循环验证码注入状态机


async def scenario_code_injection(bt: BrowserThread) -> None:
    print("\n==== 场景4：跨循环验证码注入状态机（真实 LoginSession） ====", flush=True)

    # —— a) 注入先于等待就绪 → False；随后工作循环等待 → 主循环侧注入 → 送达
    session = ls.LoginSession("sid-inject", "douyin", "acc", 60.0)
    inject_early = session.inject_code("000000")  # 竞态1：等待通道尚未建立
    received: list[str] = []

    async def _body():
        code = await session.wait_for_code(timeout=8.0)
        received.append(code)

    task = asyncio.get_running_loop().create_task(
        bt.run_browser_coro(lambda: _body(), timeout=15.0))
    ready = await _wait_until(lambda: session.status == "need_input")
    injected = session.inject_code("123456")
    try:
        await asyncio.wait_for(task, timeout=8.0)
    except asyncio.TimeoutError:
        task.cancel()
    check("跨循环注入：等待就绪前注入拒绝；就绪后主循环侧注入送达工作循环",
          inject_early is False and ready and injected is True
          and received == ["123456"] and session.status == "waiting",
          f"early={inject_early} ready={ready} injected={injected} "
          f"received={received} after_status={session.status}")

    # —— b) 竞态2：等待中途取消（模拟管理器取消语义：先置 cancelled 再取消任务）
    session2 = ls.LoginSession("sid-cancel", "douyin", "acc", 60.0)
    saw_cancel: list[bool] = []

    async def _body2():
        try:
            await session2.wait_for_code(timeout=10.0)
        except asyncio.CancelledError:
            saw_cancel.append(True)
            raise

    task2 = asyncio.get_running_loop().create_task(
        bt.run_browser_coro(lambda: _body2(), timeout=15.0))
    ready2 = await _wait_until(lambda: session2.status == "need_input")
    session2.status = "cancelled"  # 管理器 cancel() 语义：先置终态
    task2.cancel()
    try:
        await task2
    except asyncio.CancelledError:
        pass
    # 给工作循环一点时间完成 finally 收敛
    await _wait_until(lambda: session2._code_future is None, timeout=3.0)  # noqa: SLF001
    inject_late = session2.inject_code("999999")
    check("等待中途取消：工作协程收到取消、状态不被 finally 覆写、取消后注入拒绝",
          ready2 and saw_cancel == [True] and session2.status == "cancelled"
          and inject_late is False,
          f"ready={ready2} saw_cancel={saw_cancel} status={session2.status} "
          f"inject_late={inject_late}")


# ================================================================ 场景 5/6：超时收敛 + 取消传播


async def scenario_timeout_and_cancel(bt: BrowserThread) -> None:
    print("\n==== 场景5/6：超时收敛 + 取消传播（句柄 / 主循环侧链式） ====", flush=True)

    # —— a) 超时：线程内 wait_for → 协程 finally 清理、主循环及时返回不悬挂
    cleaned: list[bool] = []

    async def _slow():
        try:
            await asyncio.sleep(30.0)
        finally:
            cleaned.append(True)

    t0 = time.monotonic()
    try:
        await bt.run_browser_coro(lambda: _slow(), timeout=0.5)
        ok, detail = False, "未超时"
    except asyncio.TimeoutError:
        ok = True
        detail = f"elapsed={time.monotonic() - t0:.2f}s cleaned={cleaned}"
    except Exception as exc:  # noqa: BLE001
        ok, detail = isinstance(exc, TimeoutError), f"exc={exc!r}"
    check("超时收敛：线程内超时 → finally 清理执行、主循环不悬挂（<3s 返回）",
          ok and cleaned == [True] and (time.monotonic() - t0) < 3.0,
          detail)

    # —— b) 句柄取消：工作循环任务被取消，协程 finally 清理
    handle = BrowserTaskHandle()
    cleaned2: list[bool] = []

    async def _slow2():
        try:
            await asyncio.sleep(30.0)
        finally:
            cleaned2.append(True)

    task = asyncio.get_running_loop().create_task(
        bt.run_browser_coro(lambda: _slow2(), timeout=30.0, handle=handle))
    started = await _wait_until(lambda: handle._task is not None)  # noqa: SLF001
    cancel_ok = handle.cancel()
    try:
        await asyncio.wait_for(task, timeout=5.0)
        raised = "none"
    except asyncio.CancelledError:
        raised = "cancelled"
    check("取消句柄：cancel() 经 call_soon_threadsafe 取消工作循环任务并传播",
          started and cancel_ok and raised == "cancelled" and cleaned2 == [True],
          f"started={started} cancel={cancel_ok} raised={raised} cleaned={cleaned2}")

    # —— c) 主循环侧任务取消 → 链式取消工作循环任务（登录会话取消路径同链）
    cleaned3: list[bool] = []

    async def _slow3():
        try:
            await asyncio.sleep(30.0)
        finally:
            cleaned3.append(True)

    task3 = asyncio.get_running_loop().create_task(
        bt.run_browser_coro(lambda: _slow3(), timeout=30.0))
    await asyncio.sleep(0.3)
    task3.cancel()
    try:
        await task3
        raised3 = "none"
    except asyncio.CancelledError:
        raised3 = "cancelled"
    drained = await _wait_until(lambda: cleaned3 == [True], timeout=5.0)
    check("主循环侧任务取消 → 链式取消工作循环任务（上游 finally 有机会清理）",
          raised3 == "cancelled" and drained,
          f"raised={raised3} cleaned={cleaned3}")


# ================================================================ 场景 7：停机


async def scenario_stop(bt: BrowserThread) -> None:
    print("\n==== 场景7：stop() drain 幂等 + 停止后拒绝新任务 ====", flush=True)

    started: list[bool] = []

    async def _inflight():
        started.append(True)
        try:
            await asyncio.sleep(30.0)
        finally:
            started.append(False)

    task = asyncio.get_running_loop().create_task(
        bt.run_browser_coro(lambda: _inflight(), timeout=30.0))
    await _wait_until(lambda: started[:1] == [True])
    t0 = time.monotonic()
    bt.stop(timeout=8.0)
    stop_elapsed = time.monotonic() - t0
    # 在途任务应被取消收敛（主循环侧收到取消/异常）
    try:
        await asyncio.wait_for(task, timeout=3.0)
        end_state = "done"
    except asyncio.CancelledError:
        end_state = "cancelled"
    except Exception:  # noqa: BLE001
        end_state = "error"
    thread_dead = bt._thread is None or not bt._thread.is_alive()  # noqa: SLF001
    # 重复停止幂等 + 停止后拒绝新任务
    bt.stop(timeout=1.0)
    try:
        await bt.run_browser_coro(lambda: asyncio.sleep(0), timeout=1.0)
        rejected = False
    except RuntimeError:
        rejected = True
    check("stop() drain：在途任务取消收敛、线程结束、带超时不悬挂",
          end_state in ("cancelled", "error") and thread_dead
          and started == [True, False] and stop_elapsed < 8.0,
          f"end_state={end_state} thread_dead={thread_dead} "
          f"cleanup={started} elapsed={stop_elapsed:.2f}s")
    check("stop() 幂等（重复调用无副作用）且停止后拒绝新任务（RuntimeError）",
          rejected and thread_dead,
          f"rejected_new={rejected} thread_dead={thread_dead}")


# ================================================================ 场景 8：发布路径调度分支（评审修复 #9）
#
# 锚定 upstream_adapter 的浏览器键集合与 execute_upload 的调度分支：
# 浏览器键经 run_browser_coro（Proactor 工作循环）、bilibili 走 to_thread（同步子进程）。
# 全假注入：monkeypatch get_browser_thread 与内置上传器构造表，不拉真实浏览器。


async def scenario_publish_dispatch() -> None:
    print("\n==== 场景8：发布路径调度分支（upstream_adapter 浏览器键/同步键） ====",
          flush=True)
    from sau_wrap.agent import upstream_adapter as ua
    from sau_wrap.service import browser_thread as bt_mod

    # —— a) 锚定 _BROWSER_BUILTIN_KEYS 恰为全部内置键减 (bilibili, video)
    expect = ua._BUILTIN_KEYS - {("bilibili", "video")}
    check("锚定 _BROWSER_BUILTIN_KEYS = _BUILTIN_KEYS - {('bilibili','video')}",
          ua._BROWSER_BUILTIN_KEYS == expect,
          f"browser_keys={sorted(ua._BROWSER_BUILTIN_KEYS)}")

    # —— b) 路由：浏览器键经 run_browser_coro；bilibili 走 to_thread（不经工作线程）
    calls = {"browser": 0, "bili_thread": 0}

    class FakeBrowserThread:
        async def run_browser_coro(self, factory, timeout=None, handle=None):
            calls["browser"] += 1
            return await factory()

    async def fake_browser_uploader(*, payload, account_file, video_file, image_files):
        return "browser-ok"

    def fake_bili_uploader(*, payload, account_file, video_file, image_files):
        calls["bili_thread"] += 1
        return asyncio.to_thread(lambda: "bili-ok")

    orig_builders = dict(ua._BUILTIN_BUILDERS)
    orig_get_bt = bt_mod.get_browser_thread
    try:
        bt_mod.get_browser_thread = lambda: FakeBrowserThread()
        ua._BUILTIN_BUILDERS[("douyin", "video")] = fake_browser_uploader
        ua._BUILTIN_BUILDERS[("bilibili", "video")] = fake_bili_uploader

        r1 = await ua.execute_upload("douyin", "video", payload={}, account_file="acc")
        check("浏览器键（douyin video）经 run_browser_coro 调度（不经 to_thread）",
              r1 == "browser-ok" and calls["browser"] == 1,
              f"result={r1!r} browser_calls={calls['browser']}")

        bili_before_browser = calls["browser"]
        r2 = await ua.execute_upload("bilibili", "video", payload={}, account_file="acc")
        check("bilibili 走 to_thread（同步，不经浏览器工作线程）",
              r2 == "bili-ok" and calls["bili_thread"] == 1
              and calls["browser"] == bili_before_browser,
              f"result={r2!r} bili_thread_calls={calls['bili_thread']} "
              f"browser_calls={calls['browser']}")
    finally:
        bt_mod.get_browser_thread = orig_get_bt
        ua._BUILTIN_BUILDERS.clear()
        ua._BUILTIN_BUILDERS.update(orig_builders)


# ================================================================ main


async def main() -> int:
    bt = BrowserThread(logger=make_logger())
    try:
        await scenario_loop_types(bt)
        await scenario_serial_and_errors(bt)
        await scenario_code_injection(bt)
        await scenario_timeout_and_cancel(bt)
        await scenario_publish_dispatch()  # 评审修复 #9：发布路径调度分支（不依赖 bt 实例）
        await scenario_stop(bt)  # 内部停止实例；此后不再复用
    finally:
        bt.stop(timeout=5.0)

    total = len(RESULTS)
    failed = sum(1 for r in RESULTS if r.startswith("[FAIL]"))
    print(f"\n==== 总计：{total - failed}/{total} 通过 ====", flush=True)
    report = os.path.join(_HERE, "_verify_report_browser_thread.txt")
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(RESULTS))
    print(f"报告已写入: {report}", flush=True)
    return failed


if __name__ == "__main__":
    # 与服务形态一致：主循环使用 Selector 策略（§5.1 定案），复现根因环境
    if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(1 if asyncio.run(main()) else 0)
