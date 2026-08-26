# -*- coding: utf-8 -*-
"""浏览器专用工作线程基座（修复：Selector 主循环不支持子进程）。

**背景（根因）**：服务形态主循环按 §5.1 定案使用
``WindowsSelectorEventLoopPolicy``（host.py，规避 Session 0 Proactor 管道竞态，
不可改动）；而 patchright/playwright 启动浏览器驱动依赖
``asyncio.create_subprocess_exec``（patchright ``_transport.py``），
Windows 的 Selector 循环不支持子进程，直接抛 ``NotImplementedError``——
四平台无头登录与浏览器发布上传因此全线不可用。

**方案（不触碰全局事件循环策略）**：单一专用守护工作线程，线程内**直接构造**
``asyncio.ProactorEventLoop()``（仅线程局部 ``set_event_loop``，全局策略不变），
浏览器协程（无头登录 ``*_setup`` / 发布上传器）经
:func:`BrowserThread.run_browser_coro` 调度到该循环执行；主循环（Selector）侧
以 ``run_coroutine_threadsafe`` + ``asyncio.wrap_future`` 桥接，异常/超时/取消
均以普通异常形态传播回调用方（调用方据此落 failed 终态或收敛停机）。

**并发语义**：单工作线程 + 线程内信号量**串行排队**——登录会话与发布任务共享
同一条浏览器工作线程，先到先得依次执行。无头浏览器本身是重资源（进程/内存/
平台风控），串行化最简单可靠，避免多浏览器实例并发带来的句柄与凭证竞态；
排队等待计入 :func:`run_browser_coro` 的超时预算。

**取消机制**：
- 主循环侧任务被取消 → ``wrap_future`` 链式取消工作循环上的 task →
  协程收到 ``CancelledError``，上游 ``finally`` 有机会关闭浏览器；
- 也可持 :class:`BrowserTaskHandle` 句柄（``run_browser_coro(handle=...)``），
  ``cancel()`` 经 ``loop.call_soon_threadsafe`` 取消工作循环上的 task。

**停机**：:meth:`BrowserThread.stop` 先取消在途任务并等待其收敛（给 ``finally``
清理机会），再停止并关闭工作循环、结束线程；带超时防停机悬挂；重复停止幂等。
停止后实例不可重启（服务停机为一次性动作；测试用新实例）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

__all__ = ["BrowserTaskHandle", "BrowserThread", "get_browser_thread"]


class BrowserTaskHandle:
    """运行中浏览器协程的取消句柄（主循环侧持有，线程安全）。

    ``cancel()`` 经工作循环 ``call_soon_threadsafe`` 取消其上的 task；
    协程内 ``finally`` 得以执行（关闭浏览器等资源）。任务未开始/已结束/
    工作循环已关闭时返回 False。
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None

    def _bind(self, loop: asyncio.AbstractEventLoop, task: asyncio.Task) -> None:
        """工作循环侧绑定（仅工作线程调用）。"""
        self._loop = loop
        self._task = task

    def _unbind(self) -> None:
        self._loop = None
        self._task = None

    def cancel(self) -> bool:
        """请求取消工作循环上的浏览器协程；已调度返回 True。"""
        loop, task = self._loop, self._task
        if loop is None or task is None or task.done():
            return False
        try:
            loop.call_soon_threadsafe(task.cancel)
            return True
        except RuntimeError:  # 工作循环已关闭
            return False


class BrowserThread:
    """单一专用浏览器工作线程（线程内 Proactor 循环，支持子进程）。"""

    def __init__(self, logger: logging.Logger | None = None,
                 name: str = "sau-browser-worker") -> None:
        self._logger = logger or logging.getLogger("sau.browser_thread")
        self._name = name
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._serial: asyncio.Semaphore | None = None
        self._ready = threading.Event()
        self._start_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._stopped = False
        #: 在途工作 task 集合（仅工作循环线程读写：注册/丢弃/停机收敛均在其上）
        self._active_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ 生命周期

    def _ensure_started(self) -> None:
        """确保工作线程已启动（幂等；停止后再调用抛 RuntimeError）。

        启动失败（构造循环异常/超时）路径会复位 ``_thread``/``_ready``（评审修复 #5），
        下次调用可重建线程；``_stopped`` 仅由显式 ``stop()`` 置位，不参与启动失败复位。
        """
        if self._thread is not None and self._ready.is_set():
            if self._loop is None:
                self._reset_failed_start()
                raise RuntimeError("浏览器工作线程启动失败（循环未建立，见服务日志）")
            return
        with self._start_lock:
            if self._thread is not None and self._ready.is_set():
                if self._loop is None:
                    self._reset_failed_start()
                    raise RuntimeError("浏览器工作线程启动失败（循环未建立，见服务日志）")
                return
            if self._stopped:
                raise RuntimeError("浏览器工作线程已停止，不再接受新任务")
            if not hasattr(asyncio, "ProactorEventLoop"):
                raise RuntimeError(
                    "当前平台无 ProactorEventLoop（仅 Windows 支持浏览器工作线程）")
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._thread_main, name=self._name, daemon=True)
            self._thread.start()
            if not self._ready.wait(timeout=10.0):
                self._reset_failed_start()
                raise RuntimeError("浏览器工作线程启动超时（10 秒未就绪）")
            if self._loop is None:
                self._reset_failed_start()
                raise RuntimeError("浏览器工作线程启动失败（循环未建立，见服务日志）")

    def _reset_failed_start(self) -> None:
        """启动失败复位（评审修复 #5）：清 ``_thread``/``_loop``/``_ready`` 令下次可重建。

        仅针对构造循环失败/超时这一瞬态，避免快路径永久抛「循环未建立」；
        ``_stopped`` 不受影响（仅显式 ``stop()`` 置位）。复位为幂等赋值，
        竞态下多个调用方同时观测到失败并复位无害。
        """
        self._thread = None
        self._loop = None
        self._serial = None
        self._ready.clear()

    def warmup(self) -> None:
        """预热：确保工作线程已启动（评审修复 #8；供启动序列经 ``to_thread`` 调用）。

        首次启动需同步等待最多 10 秒；在服务启动序列提前预热，可避免首个浏览器
        请求在主循环协程内同步 ``Event.wait`` 卡住 5409/WS。幂等；启动失败异常由调用方
        兜底（配合评审修复 #5 的复位，下次请求仍可重建）。_ensure_started 本身保留。
        """
        self._ensure_started()

    def _thread_main(self) -> None:
        """工作线程主体：构造 Proactor 循环 → run_forever → 停机后关闭。"""
        loop = None
        try:
            loop = asyncio.ProactorEventLoop()
            asyncio.set_event_loop(loop)  # 仅线程局部语义，不改全局策略
            self._loop = loop
            self._serial = asyncio.Semaphore(1)  # 串行排队（单浏览器工作线程）
            self._ready.set()
            self._logger.info("浏览器工作线程就绪: loop=%s", type(loop).__name__)
            loop.run_forever()
        except Exception:  # noqa: BLE001 - 线程内顶层兜底，不得让线程裸崩
            self._logger.exception("浏览器工作线程异常退出")
            self._loop = None
        finally:
            self._ready.set()  # 确保启动方不悬挂
            if loop is not None:
                try:
                    loop.close()
                except Exception:  # pragma: no cover
                    pass

    def stop(self, timeout: float = 15.0) -> None:
        """停机 drain：取消在途任务 → 等待收敛 → 关闭循环 → 结束线程。

        幂等（重复调用无副作用）；超时仅告警不抛错（守护线程不硬杀），
        防止停机序列悬挂。停止后实例不可复用。
        """
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            thread = self._thread
        if thread is None:
            return
        deadline = time.monotonic() + max(0.5, timeout)
        loop = self._loop
        if loop is not None and loop.is_running():
            # 1) 取消在途任务并等待其 finally 清理收敛
            try:
                drain = asyncio.run_coroutine_threadsafe(self._cancel_all(), loop)
                drain.result(timeout=max(0.5, deadline - time.monotonic() - 1.0))
            except Exception:  # noqa: BLE001 - drain 超时/失败不阻断停机
                self._logger.warning("浏览器工作线程在途任务收敛超时/异常，继续停机",
                                     exc_info=True)
            # 2) 停止工作循环
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:  # pragma: no cover
                pass
        # 3) 等待线程结束
        thread.join(timeout=max(0.1, deadline - time.monotonic()))
        if thread.is_alive():
            self._logger.warning("浏览器工作线程未在 %.1f 秒内结束（守护线程，随进程退出）",
                                 timeout)
        else:
            self._logger.info("浏览器工作线程已停止")

    async def _cancel_all(self) -> None:
        """工作循环内：取消全部在途任务并等待收敛（stop 经跨线程调度）。"""
        tasks = list(self._active_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------ 任务入口

    async def run_browser_coro(self, coro_factory, timeout: float | None = None,
                               handle: BrowserTaskHandle | None = None):
        """主循环侧入口：把浏览器协程调度到工作线程的 Proactor 循环执行并等待结果。

        - ``coro_factory``：无参可调用对象，**在工作线程内**被调用以创建协程
          （保证协程的创建与 await 同循环）；
        - ``timeout``：线程内 ``asyncio.wait_for`` 包裹（含排队等待），超时后
          内层协程收到取消、``finally`` 有机会清理浏览器，主循环侧收到
          ``TimeoutError``（含排队时间在内的总预算）；
        - ``handle``：可选取消句柄，绑定后可从主循环侧取消运行中协程；
        - 协程内异常原样传播回调用方（经 ``wrap_future``）；
        - 主循环侧任务被取消时链式取消工作循环上的 task（上游 finally 清理）。
        """
        self._ensure_started()
        if self._stopped or self._loop is None:
            raise RuntimeError("浏览器工作线程已停止，不再接受新任务")
        cf_future = asyncio.run_coroutine_threadsafe(
            self._run_job(coro_factory, timeout, handle), self._loop)
        try:
            return await asyncio.wrap_future(cf_future)
        except asyncio.CancelledError:
            # 主循环侧取消 → 链式取消工作循环任务（wrap_future 已联动，此处兜底）
            cf_future.cancel()
            raise

    async def _run_job(self, coro_factory, timeout: float | None,
                       handle: BrowserTaskHandle | None):
        """工作循环侧任务包装：注册在途集合 + 串行信号量 + 超时包裹。"""
        task = asyncio.current_task()
        assert task is not None
        self._active_tasks.add(task)
        if handle is not None:
            handle._bind(asyncio.get_running_loop(), task)  # noqa: SLF001
        try:
            async def _body():
                assert self._serial is not None
                async with self._serial:  # 串行排队：登录/发布依次执行
                    return await coro_factory()

            if timeout is not None:
                return await asyncio.wait_for(_body(), timeout=timeout)
            return await _body()
        finally:
            self._active_tasks.discard(task)
            if handle is not None:
                handle._unbind()  # noqa: SLF001


# ================================================================ 全局单例

_DEFAULT_INSTANCE: BrowserThread | None = None
_DEFAULT_LOCK = threading.Lock()


def get_browser_thread() -> BrowserThread:
    """进程级共享的浏览器工作线程单例（登录会话与发布任务共用，串行排队）。"""
    global _DEFAULT_INSTANCE
    with _DEFAULT_LOCK:
        if _DEFAULT_INSTANCE is None:
            _DEFAULT_INSTANCE = BrowserThread()
        return _DEFAULT_INSTANCE
