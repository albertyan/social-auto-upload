# -*- coding: utf-8 -*-
"""真机手动冒烟：真实浏览器端到端验证「服务形态 NotImplementedError 修复 + 有头登录」（任务 #9）。

⚠️ **真机手动冒烟，不纳入自动回归**——需真实网络、本机浏览器内核
（C:\\ProgramData\\SAU\\browsers\\chromium-1208）与活跃桌面会话（有头项），
且会打开真实抖音页面。自动回归请用 ``verify_browser_thread.py``（假协程）
与 ``verify_s9.py``（假执行器）。

用法（仓库根目录、项目 .venv）::

    .venv\\Scripts\\python.exe -m sau_wrap.tests.live_browser_smoke headless-fix
    .venv\\Scripts\\python.exe -m sau_wrap.tests.live_browser_smoke contrast
    .venv\\Scripts\\python.exe -m sau_wrap.tests.live_browser_smoke headed-direct

三个子项：

- ``headless-fix``（冒烟 1）：进程开头置 ``WindowsSelectorEventLoopPolicy``
  模拟服务主循环策略，经 ``browser_thread.run_browser_coro`` 跑真实
  ``douyin_setup(headless=True, qrcode_callback=cb)``（与
  ``login_sessions._run_platform_login`` 同形状），轮询至二维码回调就绪
  （最长 90 秒）→ 断言无 NotImplementedError → 干净取消并核验无残留
  chrome 进程。
- ``contrast``（冒烟 2 对照组）：纯 Selector 循环上直接
  ``await douyin_setup(headless=True)``，预期复现 NotImplementedError
  （证明根因，与修复对照）。
- ``headed-direct``（冒烟 3 兜底）：默认策略（Windows Proactor，与
  ``headed_login`` 子进程 ``asyncio.run`` 等价环境）直接跑
  ``douyin_setup(headless=False)``，观察有头窗口启动加载 40 秒后干净取消。
  （若 ``python -m sau_wrap login-headed`` CLI 路径已在外部单独冒烟通过，
  本项作为同环境等价补充证据。）

安全纪律：

- cookie 落 ``%TEMP%\\sau_smoke_douyin.json``——**绝不触碰**
  ``%ProgramData%\\SAU\\cookies`` 下任何真实文件；
- 不做真实扫码登录：二维码就绪即取消（上游 ``finally`` 关闭浏览器）；
- 单项超时 90 秒防挂死。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# ------------------------------------------------------------ 环境引导

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 浏览器内核目录须在任何 patchright import 前设置（与 entry.py 顶部同口径）
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", r"C:\ProgramData\SAU\browsers")
# 服务形态同款适配：无桌面，抖音 cookie 复核走无头（本冒烟 account 文件不存在，
# 实际不会触发复核，此处仅为与 _run_platform_login 环境对齐）
os.environ["DOUYIN_COOKIE_AUTH_HEADLESS"] = "true"

#: 冒烟专用 cookie 路径（%TEMP%，不碰主目录）
SMOKE_COOKIE = Path(tempfile.gettempdir()) / "sau_smoke_douyin.json"

#: 单项冒烟最长预算（秒，任务约束 ≤120）
ITEM_TIMEOUT = 90.0

_T0 = time.time()


def log(msg: str) -> None:
    print(f"[{time.time() - _T0:7.1f}s] {msg}", flush=True)


def _browser_procs() -> list[str]:
    """当前 chrome/chromium 相关进程行（tasklist CSV 过滤）。"""
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception as exc:  # noqa: BLE001
        log(f"tasklist 调用失败: {exc!r}")
        return []
    return [ln for ln in out.splitlines() if "chrome" in ln.lower()]


def _cleanup_temp_artifacts() -> None:
    """清理冒烟临时产物（临时 cookie 与二维码 PNG 副本）。"""
    for p in SMOKE_COOKIE.parent.glob("sau_smoke_douyin*"):
        try:
            p.unlink()
        except OSError:
            pass


# ------------------------------------------------------------ 冒烟 1：无头修复端到端


def run_headless_fix() -> int:
    import asyncio

    # 模拟服务主循环策略（§5.1 定案：host.py 全局 Selector）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(_headless_fix_main())


async def _headless_fix_main() -> int:
    import asyncio

    from sau_wrap.service.browser_thread import BrowserTaskHandle, get_browser_thread
    from uploader.douyin_uploader.main import douyin_setup

    procs_before = _browser_procs()
    log(f"启动前 chrome 相关进程数: {len(procs_before)}")
    log(f"临时 cookie 路径: {SMOKE_COOKIE}（存在={SMOKE_COOKIE.exists()}）")

    qr = {"ready": False, "at": 0.0, "url_bytes": 0, "path": ""}

    def cb(payload: dict) -> None:  # 工作线程内被上游调用
        url = payload.get("image_data_url") or ""
        qr["ready"] = url.startswith("data:image")
        qr["at"] = time.time()
        qr["url_bytes"] = len(url)
        qr["path"] = payload.get("image_path") or ""
        log(f"★ 二维码回调触发: data_url={qr['url_bytes']}B image_path={qr['path']}")

    bt = get_browser_thread()
    bt.warmup()
    handle = BrowserTaskHandle()

    t0 = time.monotonic()
    # 与 login_sessions.default_real_executor → _run_platform_login 同形状：
    # 上游 *_setup 整体下沉浏览器工作线程（线程内 Proactor 循环）
    job = asyncio.create_task(bt.run_browser_coro(
        lambda: douyin_setup(str(SMOKE_COOKIE), handle=True, return_detail=True,
                             qrcode_callback=cb, headless=True),
        timeout=ITEM_TIMEOUT, handle=handle))

    deadline = time.monotonic() + ITEM_TIMEOUT
    while not qr["ready"] and time.monotonic() < deadline:
        if job.done():
            break
        await asyncio.sleep(0.5)

    if job.done() and not qr["ready"]:
        exc = job.exception()
        if isinstance(exc, NotImplementedError):
            log(f"✗ 失败：仍复现 NotImplementedError（修复未生效）: {exc!r}")
            return 1
        log(f"✗ 失败：二维码就绪前执行器已退出: {exc!r}")
        bt.stop(15.0)
        return 1

    if not qr["ready"]:
        log("✗ 失败：90 秒内二维码回调未触发（疑似风控页/网络问题，见上游日志）")
        handle.cancel()
        await _await_job_quiet(job)
        bt.stop(15.0)
        return 1

    log(f"✓ 无 NotImplementedError；二维码就绪（启动后 {qr['at'] - _T0:.1f}s 回调）")
    log("→ 请求取消（模拟会话取消链：主任务取消 → 桥接链式取消 → 上游 finally 关浏览器）")
    cancelled = handle.cancel()
    log(f"handle.cancel()={cancelled}")
    await _await_job_quiet(job)
    bt.stop(15.0)

    await asyncio.sleep(3.0)  # 等浏览器进程收尾
    procs_after = _browser_procs()
    log(f"取消后 chrome 相关进程数: {len(procs_after)}（前 {len(procs_before)}）")
    if len(procs_after) > len(procs_before):
        log("✗ 失败：存在残留浏览器进程:")
        for ln in procs_after:
            log(f"    {ln}")
        return 1
    log("✓ 冒烟 1 通过：无头链路端到端（Selector 主循环 + Proactor 工作线程）")
    return 0


async def _await_job_quiet(job) -> None:
    import asyncio

    try:
        result = await job
        log(f"任务正常返回（未走取消路径）: {result!r}")
    except asyncio.CancelledError:
        log("✓ 任务以 CancelledError 收敛（干净取消，上游 finally 有机会关浏览器）")
    except Exception as exc:  # noqa: BLE001
        log(f"任务异常收敛: {exc!r}")


# ------------------------------------------------------------ 冒烟 2：对照组


def run_contrast() -> int:
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    async def _main() -> int:
        from uploader.douyin_uploader.main import douyin_setup

        procs_before = _browser_procs()
        log(f"对照组启动前 chrome 相关进程数: {len(procs_before)}")
        t0 = time.monotonic()
        try:
            # 纯 Selector 循环直接跑（不经浏览器工作线程）→ 预期根因复现
            await asyncio.wait_for(
                douyin_setup(str(SMOKE_COOKIE), handle=True, return_detail=True,
                             headless=True),
                timeout=60.0)
            log("✗ 意外：未出现 NotImplementedError（与预期不符，需排查）")
            return 1
        except NotImplementedError as exc:
            log(f"✓ 预期复现 NotImplementedError（{time.monotonic() - t0:.2f}s）: {exc!r}")
        except Exception as exc:  # noqa: BLE001
            log(f"✗ 出现非 NotImplementedError 异常（与预期不符）: {exc!r}")
            return 1
        procs_after = _browser_procs()
        log(f"对照组结束进程数: {len(procs_after)}（前 {len(procs_before)}）"
            f"——子进程创建在发起点即失败，无浏览器拉起开销")
        log("✓ 冒烟 2 通过：对照组证实根因（Selector 循环不支持子进程）")
        return 0

    return asyncio.run(_main())


# ------------------------------------------------------------ 冒烟 3：有头直连（兜底）


def run_headed_direct() -> int:
    import asyncio

    # 默认策略（Windows 即 Proactor）——与 headed_login 子进程 asyncio.run 等价环境

    async def _main() -> int:
        from uploader.douyin_uploader.main import douyin_setup

        procs_before = _browser_procs()
        log(f"有头冒烟启动前 chrome 相关进程数: {len(procs_before)}")

        seen = threading.Event()

        def cb(payload: dict) -> None:
            url = payload.get("image_data_url") or ""
            if url.startswith("data:image"):
                log(f"★ 有头窗口内二维码已就绪（{len(url)}B）——登录页加载成功")
                seen.set()

        job = asyncio.create_task(douyin_setup(
            str(SMOKE_COOKIE), handle=True, return_detail=True,
            qrcode_callback=cb, headless=False))

        deadline = time.monotonic() + 40.0
        while time.monotonic() < deadline:
            if job.done():
                break
            await asyncio.sleep(1.0)

        if job.done():
            exc = job.exception()
            if exc is not None:
                log(f"✗ 有头链路提前异常退出: {exc!r}")
                return 1
            log(f"有头链路提前返回（登录意外完成？）: {job.result()!r}")
            return 0

        obs = "二维码已就绪" if seen.is_set() else "浏览器进程已拉起、等待二维码/页面加载"
        log(f"✓ 有头浏览器运行 40 秒未崩（{obs}）→ 干净取消收尾")
        job.cancel()
        await _await_job_quiet(job)

        await asyncio.sleep(3.0)
        procs_after = _browser_procs()
        log(f"取消后 chrome 相关进程数: {len(procs_after)}（前 {len(procs_before)}）")
        if len(procs_after) > len(procs_before):
            log("✗ 失败：存在残留浏览器进程:")
            for ln in procs_after:
                log(f"    {ln}")
            return 1
        log("✓ 冒烟 3（兜底项）通过：有头窗口可启动加载并干净关闭")
        return 0

    return asyncio.run(_main())


# ------------------------------------------------------------ 入口


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in ("headless-fix", "contrast", "headed-direct"):
        print(__doc__)
        return 2
    item = argv[0]
    log(f"==== 冒烟项: {item}（单项预算 {ITEM_TIMEOUT:.0f}s）====")
    try:
        if item == "headless-fix":
            rc = run_headless_fix()
        elif item == "contrast":
            rc = run_contrast()
        else:
            rc = run_headed_direct()
    finally:
        _cleanup_temp_artifacts()
    log(f"==== 冒烟项 {item} 结束: {'通过' if rc == 0 else '失败'} (exit={rc}) ====")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
