# -*- coding: utf-8 -*-
"""浏览器内核下载与安装（设计文档 §8.7 三层方案，实施计划 S8）。

根因：patchright/playwright 默认从微软 CDN 下载内核，国内网络常缓慢或
「卡着」不报错。三层设计：

1. **镜像源策略**：默认 ``PLAYWRIGHT_DOWNLOAD_HOST`` 指向 npmmirror 国内镜像，
   官方源作回退；
2. **下载可靠性**：60 秒无输出判定卡死 → 自动断开 → 镜像源轮换
   （镜像 → 官方 → 镜像，最多 3 轮）；
3. **离线兜底**：``sau.exe browser install --from-file <zip>``——本地内核
   zip 直接解压进 ``%ProgramData%\\SAU\\browsers\\``（覆盖内网/无外网）。

安装位置决策（任务 #19 ②-2）：统一装到 ``%ProgramData%\\SAU\\browsers\\``
（§3.6 ``browsers/``），通过 ``PLAYWRIGHT_BROWSERS_PATH`` 环境变量让
patchright 查找该目录——``entry.main`` 启动即 ``setdefault``，服务
（SYSTEM）/托盘/CLI 全路径一致；不用默认 ``%USERPROFILE%`` 缓存（SYSTEM
服务与用户会话的 USERPROFILE 不同，默认位置服务进程找不到）。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
import zipfile
from pathlib import Path

from sau_wrap import paths

#: npmmirror 国内镜像（tray 分支已验证可用，§8.7 第一层）
MIRROR_HOST = "https://npmmirror.com/mirrors/playwright"

#: 卡死判定窗口（秒，§8.7 第二层：60 秒无字节进展）
STALL_SECONDS = 60.0

#: 源轮换顺序（镜像 → 官方 → 镜像，最多 3 轮，§8.7 第二层）
SOURCE_ROTATION: tuple[str | None, ...] = (MIRROR_HOST, None, MIRROR_HOST)


def _browsers_dir() -> Path:
    return paths.ensure_dir(paths.BROWSERS_DIR)


# ---------------------------------------------------------------- 内核元数据


def _driver_cmd() -> list[str]:
    """patchright CLI 驱动命令（[node, cli.js]），兼容源码与打包形态。"""
    from patchright._impl._driver import compute_driver_executable  # noqa: PLC0415

    node, cli = compute_driver_executable()
    return [str(node), str(cli)]


def chromium_revision() -> str:
    """当前 patchright 期望的 chromium revision（读包内 browsers.json）。"""
    import patchright  # noqa: PLC0415

    bj = Path(patchright.__file__).parent / "driver" / "package" / "browsers.json"
    data = json.loads(bj.read_text(encoding="utf-8"))
    for b in data["browsers"]:
        if b["name"] == "chromium":
            return str(b["revision"])
    raise RuntimeError("browsers.json 中未找到 chromium 条目")


def chromium_dir() -> Path:
    """内核安装目录（patchright 查找约定：{PLAYWRIGHT_BROWSERS_PATH}/{name}-{rev}）。"""
    return _browsers_dir() / f"chromium-{chromium_revision()}"


def chromium_executable() -> Path:
    """chromium 可执行文件（Windows 布局：chrome-win\\chrome.exe）。"""
    return chromium_dir() / "chrome-win" / "chrome.exe"


def is_installed() -> bool:
    """doctor / 首启按需判定：内核目录含完成标记或可执行文件存在。"""
    d = chromium_dir()
    if not d.is_dir():
        return False
    if (d / "INSTALLATION_COMPLETE").exists():
        return True
    return chromium_executable().is_file()


# ---------------------------------------------------------------- 在线下载


def _run_install_round(host: str | None, logger: logging.Logger) -> bool:
    """执行一轮 ``patchright install chromium``；60 秒无输出判卡死杀进程。

    返回是否成功（exit 0）。
    """
    env = dict(os.environ)
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(_browsers_dir())
    if host:
        env["PLAYWRIGHT_DOWNLOAD_HOST"] = host
    else:
        env.pop("PLAYWRIGHT_DOWNLOAD_HOST", None)  # 官方源回退
    src = host or "官方源（playwright CDN）"
    logger.info("browser install 轮次开始（源=%s）", src)
    try:
        proc = subprocess.Popen(
            _driver_cmd() + ["install", "chromium"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
    except OSError as exc:
        logger.error("无法启动 patchright 驱动：%s", exc)
        return False

    out_q: "queue.Queue[str | None]" = queue.Queue()

    def _reader() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                out_q.put(line)
        finally:
            out_q.put(None)

    threading.Thread(target=_reader, daemon=True).start()
    last_activity = time.monotonic()
    while True:
        if proc.poll() is not None:
            while not out_q.empty():
                line = out_q.get_nowait()
                if line:
                    logger.info("[patchright] %s", line.rstrip())
            return proc.returncode == 0
        try:
            line = out_q.get(timeout=1.0)
        except queue.Empty:
            line = ""
        if line is None:
            continue
        if line:
            logger.info("[patchright] %s", line.rstrip())
            last_activity = time.monotonic()
        if time.monotonic() - last_activity > STALL_SECONDS:
            logger.warning("%s 秒无进展，判定卡死 → 断开换源（§8.7）",
                           int(STALL_SECONDS))
            try:
                proc.kill()
            except OSError:
                pass
            proc.wait(timeout=10)
            return False


def install_online(logger: logging.Logger) -> bool:
    """在线安装：镜像 → 官方 → 镜像轮换，最多 3 轮（§8.7 第二层）。"""
    if is_installed():
        logger.info("浏览器内核已安装：%s", chromium_dir())
        return True
    for i, host in enumerate(SOURCE_ROTATION, 1):
        logger.info("第 %d/%d 轮（%s）", i, len(SOURCE_ROTATION),
                    host or "官方源")
        if _run_install_round(host, logger):
            if is_installed():
                logger.info("浏览器内核安装成功：%s", chromium_executable())
                return True
            logger.warning("patchright 报告成功但内核目录不完整，继续下一轮")
    logger.error("浏览器内核安装失败（3 轮均失败）：请检查网络，"
                 "或使用离线安装 sau browser install --from-file <zip>")
    return False


# ---------------------------------------------------------------- 离线安装


def _extract_safe(zf: zipfile.ZipFile, dest: Path, logger: logging.Logger) -> None:
    """防 zip-slip 解压（离线包常以管理员执行，必须净化路径）。

    本构建环境（Python 3.12.11）的 zipfile 未提供 ``extractall(filter=…)``
    （无 ``zipfile.data_filter``，实测），故手动校验：拒绝绝对路径/盘符/
    UNC、拒绝解析后跳出目标目录的条目、忽略目录条目与反斜杠变体（zip 规范
    用正斜杠）。
    """
    dest = dest.resolve()
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if not name or name.endswith("/"):
            continue  # 空名/目录条目
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name) \
                or name.startswith("//") or ".." in name.split("/"):
            raise ValueError(f"离线包含不安全路径条目：{info.filename}")
        target = (dest / name).resolve()
        if not str(target).startswith(str(dest) + os.sep) and target != dest:
            raise ValueError(f"离线包路径穿越条目：{info.filename}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            while chunk := src.read(1024 * 1024):
                out.write(chunk)


def install_from_file(zip_path: Path, logger: logging.Logger) -> bool:
    """离线安装（§8.7 第三层）：本地内核 zip 解压进 browsers 目录。

    兼容两种 zip 布局：① 顶层即 ``chromium-{rev}/…``（官方下载包原样）；
    ② 顶层为 ``chrome-win/…`` 等内容（自动归位到 ``chromium-{rev}/``）。
    解压后补写 ``INSTALLATION_COMPLETE`` 标记（patchright 查找约定）。
    """
    zip_path = Path(zip_path).expanduser().resolve()
    if not zip_path.is_file():
        logger.error("离线包不存在：%s", zip_path)
        return False
    target = chromium_dir()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            if not names:
                logger.error("离线包为空：%s", zip_path)
                return False
            expected = f"chromium-{chromium_revision()}/"
            has_root = any(n.replace("\\", "/").startswith(expected)
                           for n in names)
            # 防 zip-slip 任意路径写（离线包常以管理员执行，§8.7）：
            # 逐条目净化校验后再解压（见 _extract_safe）
            if has_root:
                _extract_safe(zf, _browsers_dir(), logger)
            else:
                paths.ensure_dir(target)
                _extract_safe(zf, target, logger)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        logger.error("离线包解压失败（含不安全条目时报 ValueError，§8.7）：%s", exc)
        return False
    (target / "INSTALLATION_COMPLETE").write_text("", encoding="utf-8")
    logger.info("离线安装完成：%s（可执行文件 %s）",
                target,
                "存在" if chromium_executable().is_file() else "缺失，请核对包内容")
    return is_installed()
