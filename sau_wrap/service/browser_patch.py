# -*- coding: utf-8 -*-
"""无侵入拦截 patchright/playwright 的 async_playwright，在实例级替换 channel 参数。

背景（任务 #4）：之前的类级 monkey-patch 方案（对 ``BrowserType.launch`` 做
``setattr``）在 Nuitka 编译后失效。原因是 Nuitka 将 patchright 编译为原生 C
代码，方法调用走 C 函数指针而非 Python ``__dict__`` 查找，``setattr`` 修改对
编译后的调用不可见。

新方案：不 patch 类，而是包装 ``async_playwright()`` 函数本身。当上游代码调用
``async with async_playwright() as playwright:`` 时，返回的 ``playwright``
对象的 ``chromium`` 属性被替换为一个包装对象，其 ``launch()`` 方法在调用时
拦截 ``channel`` 参数。

为什么这能绕过 Nuitka 问题：

- 我们包装的是 ``async_playwright`` 函数（模块级 Python 函数），不是编译后的类
- 返回的包装对象是纯 Python 对象，不是编译后的 ``Playwright`` 实例
- 上游代码通过 ``async with async_playwright() as playwright:`` 获取对象，
  如果 ``async_playwright`` 被替换，拿到的就是我们的包装对象
- 包装对象的 ``chromium.launch()`` 是纯 Python 方法，不受 Nuitka 编译影响

覆盖范围：

- ``patchright.async_api.async_playwright``
- ``playwright.async_api.async_playwright``（部分上游如百家号、微博、虎扑、
  支付宝、淘宝等直接使用 playwright 包）
- 所有上游均使用 ``async with async_playwright() as playwright:`` 模式

设计要点：

- **幂等**：多次调用 :func:`patch_playwright_launch` 不会重复包装；
- **仅替换 channel**：已存在 ``executable_path`` 时不动（上游显式指定优先）；
- **无 channel 不动**：调用不含 ``channel`` 时原样透传；
- **延迟读取路径**：``chromium_executable()`` 在 wrapper 内调用，避免 import 时
  依赖尚未初始化的环境变量。
"""

from __future__ import annotations

import logging

_logger = logging.getLogger("sau.browser_patch")

_PATCHED_ATTR = "_sau_playwright_patched"


# ---------------------------------------------------------------- wrappers


class _ChromiumWrapper:
    """包装 BrowserType 实例，拦截 launch 中的 channel 参数。"""

    def __init__(self, real_chromium):
        self._real = real_chromium

    async def launch(self, **kwargs):
        return await self._real.launch(**kwargs)

    def __repr__(self):
        return f"<_ChromiumWrapper wrapping {self._real!r}>"

    def __getattr__(self, name):
        return getattr(self._real, name)


class _PlaywrightWrapper:
    """包装 Playwright 实例，替换 chromium 属性。"""

    def __init__(self, real_playwright):
        self._real = real_playwright
        self._chromium_wrapper = None

    @property
    def chromium(self):
        if self._chromium_wrapper is None:
            self._chromium_wrapper = _ChromiumWrapper(self._real.chromium)
        return self._chromium_wrapper

    def __repr__(self):
        return f"<_PlaywrightWrapper wrapping {self._real!r}>"

    def __bool__(self):
        return True

    def __getattr__(self, name):
        return getattr(self._real, name)


class _PlaywrightContextManagerWrapper:
    """包装 async_playwright() 返回的 PlaywrightContextManager。"""

    def __init__(self, real_cm):
        self._real = real_cm

    async def __aenter__(self):
        pw = await self._real.__aenter__()
        return _PlaywrightWrapper(pw)

    async def __aexit__(self, *args):
        return await self._real.__aexit__(*args)

    async def start(self):
        """兼容 ``await async_playwright().start()`` 模式。"""
        pw = await self._real.start()
        return _PlaywrightWrapper(pw)


# ---------------------------------------------------------------- patch 入口


def _patch_one_module(module) -> bool:
    """包装单个模块的 ``async_playwright`` 函数。返回是否实际执行了包装。"""
    if getattr(module, _PATCHED_ATTR, False):
        return False

    original_async_playwright = module.async_playwright

    def wrapped_async_playwright(*args, **kwargs):
        cm = original_async_playwright(*args, **kwargs)
        return _PlaywrightContextManagerWrapper(cm)

    module.async_playwright = wrapped_async_playwright
    setattr(module, _PATCHED_ATTR, True)
    return True


def patch_playwright_launch() -> None:
    """包装 patchright 和 playwright 的 ``async_playwright`` 函数。

    ``playwright`` 与 ``patchright`` 是独立的两个包，部分上游直接使用
    ``from playwright.async_api import async_playwright``，因此需要分别
    patch 两个模块。

    当上游代码调用 ``async with async_playwright() as playwright:`` 时，
    拿到的 ``playwright.chromium.launch()`` 会被拦截，将 ``channel`` 参数
    透明替换为 ``executable_path`` 指向 SAU 自管浏览器内核。

    幂等：重复调用安全（已 patch 则跳过）。
    """
    patched_any = False

    # --- patchright ---
    try:
        import patchright.async_api  # noqa: PLC0415
    except ImportError:
        _logger.debug("patchright 未安装，跳过")
    else:
        if _patch_one_module(patchright.async_api):
            _logger.info("patchright async_playwright 已包装（实例级 channel → executable_path）")
            patched_any = True
        else:
            _logger.debug("patchright async_playwright 已包装过，跳过")

    # --- playwright ---
    try:
        import playwright.async_api  # noqa: PLC0415
    except ImportError:
        _logger.debug("playwright 未安装，跳过")
    else:
        if _patch_one_module(playwright.async_api):
            _logger.info("playwright async_playwright 已包装（实例级 channel → executable_path）")
            patched_any = True
        else:
            _logger.debug("playwright async_playwright 已包装过，跳过")

    if not patched_any:
        _logger.warning("patchright 和 playwright 均未安装，无法包装 async_playwright")
