# -*- coding: utf-8 -*-
"""浏览器内核下载与安装（设计文档 §8.7 三层方案，实施计划 S8）。

根因：patchright/playwright 默认从微软 CDN 下载内核，国内网络常缓慢或
「卡着」不报错。三层设计：

1. **镜像源策略**：首选 npmmirror 的 Chrome for Testing 直链**自管下载**
   （任务 #26 实测：``PLAYWRIGHT_DOWNLOAD_HOST`` 的 playwright 镜像路径对新版
   内核 404，故直链自下；字节级进度/卡死判定完全可控），官方源经 patchright
   驱动作回退；
2. **下载可靠性**：60 秒无字节进展判定卡死 → 自动断开 → 源轮换（直链镜像 →
   官方 patchright → 直链镜像，共 3 轮）；
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
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from sau_wrap import paths

#: npmmirror Chrome for Testing 直链模板（任务 #26 实测有 145.0.7632.6）：
#: registry.npmmirror.com 的 CFT 二进制镜像，不经 PLAYWRIGHT_DOWNLOAD_HOST
CFT_MIRROR_URL_TEMPLATE = ("https://registry.npmmirror.com/-/binary/"
                           "chrome-for-testing/{version}/win64/{zip}")

#: 直链下载组件清单（任务 #26 实测：headless=True 需 chromium_headless_shell，
#: patchright registry 目录名用下划线、CFT zip 顶层目录用短横线）：
#: (目标目录前缀, zip 文件名, 解压后可执行文件相对路径)
DIRECT_COMPONENTS: tuple[tuple[str, str, str], ...] = (
    ("chromium-{rev}", "chrome-win64.zip", "chrome-win64/chrome.exe"),
    ("chromium_headless_shell-{rev}", "chrome-headless-shell-win64.zip",
     "chrome-headless-shell-win64/chrome-headless-shell.exe"),
)

#: 卡死判定窗口（秒，§8.7 第二层：60 秒无字节进展）
STALL_SECONDS = 60.0

#: 总时限（秒，任务 #26 决策变更：安装时自动下载，弱网上限 20 分钟，
#: 超时按失败处理——安装不阻断，§17「内核下载失败不阻断」定案沿用）
TOTAL_TIMEOUT_SECONDS = 20 * 60.0

#: 源轮换顺序（直链镜像自管下载 → 官方源 patchright → 直链镜像重试，
#: 共 3 轮，§8.7 第二层；任务 #26 调整：原 playwright 镜像路径对新版 404）
SOURCE_ROTATION: tuple[str | None, ...] = ("direct", None, "direct")

#: HEAD 失败降级时按组件折算总百分比的区间（组件 1=0~60%、组件 2=60~100%，
#: 比例约 173:114，任务 #26 实测口径；任务 #10）
FALLBACK_PERCENT_SPANS: tuple[tuple[int, int], ...] = ((0, 60), (60, 100))

#: 进度文件上报节流（秒）：安装器 500ms 轮询，1.5s 粒度兼顾心跳与开销（任务 #10）
PROGRESS_REPORT_INTERVAL = 1.5


def _ascii_value(v: object) -> str:
    """进度文件值域强制纯 ASCII（任务 #10：Inno 对无 BOM 文件按 GBK 解析，
    非 ASCII 值会乱码；非 ASCII 字符转 \\xXXXX 转义，安装器只读数字/枚举字段）。"""
    return str(v).encode("ascii", "backslashreplace").decode("ascii")


class ProgressReporter:
    """安装器进度文件上报器（任务 #10，§8.7/§7.3 安装页进度条）。

    进度文件为 INI 格式（路径由安装器经 ``--progress-file`` 显式传入，默认
    ``%ProgramData%\\SAU\\download_progress.ini``）：``[progress]`` 字段增量
    合并、每次写盘 seq 自增作心跳（安装器以心跳停滞判下载进程崩溃）；
    ``[result]`` 的 done/exit_code 由 :meth:`finish` 写入（进程正常结束，
    含失败，安装器以 done 判定收尾）。

    可靠性纪律：
    - 原子写（写同目录 .tmp → ``os.replace``）——安装器 500ms 轮询绝不读半截文件；
    - **所有写操作吞掉 OSError**——进度上报失败绝不影响下载主链路；
    - 值域纯 ASCII（见 :func:`_ascii_value`，GBK 解析约束）。
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._fields: dict[str, str] = {}
        self._seq = 0
        self._done = 0
        self._exit_code: int | None = None

    def update(self, **fields: object) -> None:
        """增量合并字段并原子写盘（seq 自增；未传字段保留旧值）。"""
        self._seq += 1
        for k, v in fields.items():
            self._fields[k] = _ascii_value(v)
        self._write()

    def finish(self, exit_code: int) -> None:
        """进程结束（正常/失败均）：写 ``[result] done=1`` + ``exit_code``。"""
        self._done = 1
        self._exit_code = int(exit_code)
        self._write()

    def _write(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            lines = ["[progress]"]
            for k in sorted(self._fields):
                lines.append(f"{k}={self._fields[k]}")
            lines.append(f"seq={self._seq}")
            lines.append("[result]")
            lines.append(f"done={self._done}")
            if self._exit_code is not None:
                lines.append(f"exit_code={self._exit_code}")
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="ascii")
            os.replace(tmp, self._path)  # 原子替换：轮询方永远读不到半截文件
        except OSError:
            pass  # 进度上报失败静默降级，绝不影响下载主链路（任务 #10）


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
    """chromium 可执行文件（CFT 布局 ``chrome-win64\\chrome.exe``，任务 #26 锤实）。"""
    return chromium_dir() / "chrome-win64" / "chrome.exe"


def headless_shell_dir() -> Path:
    """headless shell 安装目录（任务 #26：登录 headless 必需）。"""
    return _browsers_dir() / f"chromium_headless_shell-{chromium_revision()}"


def headless_shell_executable() -> Path:
    """headless shell 可执行文件（CFT 布局）。"""
    return headless_shell_dir() / "chrome-headless-shell-win64" / "chrome-headless-shell.exe"


def chromium_browser_version() -> str:
    """browsers.json 的 browserVersion（CFT 版本号，直链镜像 URL 用）。"""
    import patchright  # noqa: PLC0415

    bj = Path(patchright.__file__).parent / "driver" / "package" / "browsers.json"
    data = json.loads(bj.read_text(encoding="utf-8"))
    for b in data["browsers"]:
        if b["name"] == "chromium":
            return str(b["browserVersion"])
    raise RuntimeError("browsers.json 中未找到 chromium 条目")


def is_installed() -> bool:
    """doctor / 首启按需判定：两组件（完整内核 + headless shell）均就位。

    任务 #26 实测：登录走 headless，patchright 需要 ``chromium_headless_shell``；
    任一缺失即视为未安装（登录会报 Executable doesn't exist）。
    """
    for d, exe in ((chromium_dir(), chromium_executable()),
                   (headless_shell_dir(), headless_shell_executable())):
        if not d.is_dir():
            return False
        if not (d / "INSTALLATION_COMPLETE").exists() and not exe.is_file():
            return False
    return True


# ---------------------------------------------------------------- 在线下载


def _run_install_round(host: str | None, logger: logging.Logger,
                      deadline: float,
                      reporter: ProgressReporter | None = None) -> bool:
    """执行一轮 ``patchright install chromium``；60 秒无输出判卡死杀进程。

    ``deadline`` 为总时限壁钟（任务 #26：弱网安装上限 20 分钟）：
    超过即杀进程判本轮失败。返回是否成功（exit 0）。

    ``reporter``（任务 #10）：best-effort 用正则 ``(\\d+)\\s*%`` 从 patchright
    输出行解析百分比上报；解析不到只刷 seq+message 保心跳。
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
            # readline() 无预读；禁用 `for line in proc.stdout` 迭代器（预读缓冲滞留行，
            # 任务 #26 实测误判卡死根因）
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                out_q.put(line)
        finally:
            out_q.put(None)

    threading.Thread(target=_reader, daemon=True).start()
    last_activity = time.monotonic()
    last_report = 0.0  # 进度上报节流壁钟（任务 #10）
    while True:
        if proc.poll() is not None:
            while not out_q.empty():
                line = out_q.get_nowait()
                if line:
                    logger.info("[patchright] %s", line.rstrip())
            return proc.returncode == 0
        try:
            # 注意：禁用 `for line in proc.stdout` 迭代器——其预读缓冲会延迟返回行，
            # 进度条 \r 段滞留缓冲 → last_activity 不刷新 → 误判卡死杀掉正常下载；
            # readline() 无预读（任务 #26 实测锤实）。
            line = out_q.get(timeout=1.0)
        except queue.Empty:
            line = ""
        if line is None:
            continue
        if line:
            logger.info("[patchright] %s", line.rstrip())
            last_activity = time.monotonic()
            if reporter is not None:
                # best-effort 解析 patchright 进度行（形如 "47%"）上报总百分比；
                # 解析不到则按节流只刷 seq+message 保心跳（安装器停滞判崩溃）
                m = re.search(r"(\d+)\s*%", line)
                now_r = time.monotonic()
                if m:
                    reporter.update(stage="downloading", source="official",
                                    percent=min(99, int(m.group(1))),
                                    message="patchright download")
                    last_report = now_r
                elif now_r - last_report >= PROGRESS_REPORT_INTERVAL:
                    reporter.update(stage="downloading", source="official",
                                    message="patchright heartbeat")
                    last_report = now_r
        if time.monotonic() > deadline:
            logger.warning("总时限（%d 分钟）已到，断开本轮（任务 #26）",
                           int(TOTAL_TIMEOUT_SECONDS // 60))
            try:
                proc.kill()
            except OSError:
                pass
            proc.wait(timeout=10)
            return False
        if time.monotonic() - last_activity > STALL_SECONDS:
            logger.warning("%s 秒无进展，判定卡死 → 断开换源（§8.7）",
                           int(STALL_SECONDS))
            try:
                proc.kill()
            except OSError:
                pass
            proc.wait(timeout=10)
            return False


def install_online(logger: logging.Logger,
                   reporter: ProgressReporter | None = None) -> bool:
    """在线安装：直链镜像 → 官方源 → 直链镜像重试，共 3 轮（§8.7 第二层）。

    任务 #26 决策变更：安装时自动下载——已装跳过（升级不重复下载）；
    总时限 20 分钟，超时按失败处理（安装不阻断）。
    任务 #26 实测调整：playwright 镜像路径对新版内核 404，镜像轮改为
    npmmirror CFT 直链自管下载（字节级卡死判定），官方源经 patchright 回退。
    """
    if is_installed():
        logger.info("浏览器内核已安装，跳过下载：%s", chromium_dir())
        return True
    t_start = time.monotonic()
    deadline = t_start + TOTAL_TIMEOUT_SECONDS
    for i, src in enumerate(SOURCE_ROTATION, 1):
        if time.monotonic() > deadline:
            logger.error("总时限（%d 分钟）已到，终止下载（任务 #26）",
                         int(TOTAL_TIMEOUT_SECONDS // 60))
            break
        # 换源轮次切换处（任务 #10）：首轮 downloading、后续轮 retrying + source；
        # percent 如实回退（安装器侧容忍，只关注心跳与终态）
        if reporter is not None:
            reporter.update(stage="retrying" if i > 1 else "downloading",
                            source="mirror" if src == "direct" else "official",
                            message=f"round {i}/{len(SOURCE_ROTATION)}")
        if src == "direct":
            logger.info("第 %d/%d 轮（npmmirror CFT 直链，自管下载）",
                        i, len(SOURCE_ROTATION))
            ok = _direct_download_round(logger, deadline, reporter)
        else:
            logger.info("第 %d/%d 轮（官方源，patchright 驱动）",
                        i, len(SOURCE_ROTATION))
            ok = _run_install_round(src, logger, deadline, reporter)
        if ok:
            if is_installed():
                logger.info("浏览器内核安装成功：%s", chromium_executable())
                return True
            logger.warning("本轮报告成功但内核目录不完整，继续下一轮")
    logger.error("浏览器内核安装失败（3 轮均失败）：请检查网络，"
                 "或使用离线安装 sau browser install --from-file <zip>")
    return False


def _head_content_length(url: str) -> int:
    """HEAD 取 Content-Length（任务 #10 总百分比基准）；失败返回 0 降级。"""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 镜像白名单域名，§8.7
            return int(resp.headers.get("Content-Length") or 0)
    except (OSError, ValueError):
        return 0


def _direct_download_round(logger: logging.Logger, deadline: float,
                           reporter: ProgressReporter | None = None) -> bool:
    """npmmirror CFT 直链自管下载（任务 #26）：两组件字节级进度下载。

    依次下载完整内核与 headless shell（登录 headless 必需）；卡死保护靠
    套接字读超时（半死连接最多 30s 即抛异常换源）；单组件已装则跳过。
    下载临时文件 → 校验 → 复用离线解压链路落盘。

    任务 #10：轮次开始对两个组件 URL 发 HEAD 取 Content-Length 求和作
    total_bytes → 真实总百分比；HEAD 失败降级按组件折算（组件 1=0~60%、
    组件 2=60~100%，:data:`FALLBACK_PERCENT_SPANS`）；每进入一个组件更新
    component 字段。
    """
    try:
        version = chromium_browser_version()
        rev = chromium_revision()
    except Exception as exc:  # noqa: BLE001 包损坏/版本读取失败：本轮直接失败
        logger.error("读取 chromium 版本信息失败：%s", exc)
        return False
    comp_count = len(DIRECT_COMPONENTS)
    # 轮次开始：HEAD 求和作总字节基准（任一失败整体降级按组件折算，任务 #10）
    urls = [CFT_MIRROR_URL_TEMPLATE.format(version=version, zip=zip_name)
            for _, zip_name, _ in DIRECT_COMPONENTS]
    head_sizes = [_head_content_length(u) for u in urls]
    grand_total = sum(head_sizes) if all(s > 0 for s in head_sizes) else 0
    logger.info("进度基准：HEAD 求和 %d 字节（%s）",
                grand_total, "真实总百分比" if grand_total else "失败，降级按组件折算")
    stats = {"done_bytes": 0}  # 已完成组件累计字节（真实总百分比模式用）
    for idx, ((dir_tpl, zip_name, exe_rel), url, head_size) in enumerate(
            zip(DIRECT_COMPONENTS, urls, head_sizes), 1):
        if time.monotonic() > deadline:
            logger.warning("总时限（%d 分钟）已到，断开直链下载（任务 #26）",
                           int(TOTAL_TIMEOUT_SECONDS // 60))
            return False
        target = _browsers_dir() / dir_tpl.format(rev=rev)
        if reporter is not None:
            # 每进入一个组件更新 component 字段（安装器可展示组件维度）
            reporter.update(component=idx, components=comp_count,
                            stage="downloading", source="mirror")
        if (target / "INSTALLATION_COMPLETE").exists() \
                and (target / exe_rel.replace("/", os.sep)).is_file():
            logger.info("组件已就位，跳过：%s", target.name)
            if grand_total:
                stats["done_bytes"] += head_size  # 已装组件按 HEAD 体积计为已完成
            continue
        if not _download_one(url, target, exe_rel, logger, deadline,
                             reporter=reporter, comp_idx=idx, comp_count=comp_count,
                             grand_total=grand_total, stats=stats):
            return False
    return is_installed()


def _download_one(url: str, target: Path, exe_rel: str,
                  logger: logging.Logger, deadline: float,
                  reporter: ProgressReporter | None = None,
                  comp_idx: int = 1, comp_count: int = 1,
                  grand_total: int = 0,
                  stats: dict | None = None) -> bool:
    """下载单个组件 zip 并落盘到 target 目录（zip 顶层内容直接归位）。

    任务 #10：``grand_total`` > 0 时按「已完成字节 + 本轮已收字节」计算真实总百分比；
    否则降级按组件区间折算（:data:`FALLBACK_PERCENT_SPANS`）。上报节流
    :data:`PROGRESS_REPORT_INTERVAL`（安装器 500ms 轮询，原 5 秒粒度太粗）。
    """
    logger.info("直链下载开始：%s", url)
    tmp = Path(tempfile.gettempdir()) / f"sau-{Path(url).name}.part"
    span_lo, span_hi = FALLBACK_PERCENT_SPANS[min(comp_idx, len(FALLBACK_PERCENT_SPANS)) - 1]
    last_log = 0.0
    try:
        # timeout=30：连接与套接字读超时（半死连接 30s 无字节即抛异常换源，§8.7）
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 镜像白名单域名，§8.7
            total = int(resp.headers.get("Content-Length") or 0)
            received = 0
            with open(tmp, "wb") as out:
                while True:
                    if time.monotonic() > deadline:
                        logger.warning("总时限（%d 分钟）已到，断开直链下载（任务 #26）",
                                       int(TOTAL_TIMEOUT_SECONDS // 60))
                        return False
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
                    received += len(chunk)
                    now = time.monotonic()
                    if now - last_log >= PROGRESS_REPORT_INTERVAL:
                        if total:
                            logger.info("直链下载进度：%d%%（%d/%d MB）",
                                        received * 100 // total,
                                        received // 1048576, total // 1048576)
                        else:
                            logger.info("直链下载进度：%d MB（未知总量）",
                                        received // 1048576)
                        if reporter is not None:
                            # 同步上报进度文件（安装器 500ms 轮询）：真实总字节基准存在时
                            # 用总百分比，否则按组件区间折算
                            if grand_total:
                                pct = min(99, ((stats or {}).get("done_bytes", 0) + received)
                                          * 100 // grand_total)
                                rep_total = grand_total
                            else:
                                pct = (span_lo + (span_hi - span_lo) * received // total
                                       if total else span_lo)
                                rep_total = total
                            reporter.update(stage="downloading", source="mirror",
                                            component=comp_idx, components=comp_count,
                                            percent=pct,
                                            downloaded_bytes=(stats or {}).get("done_bytes", 0) + received,
                                            total_bytes=rep_total,
                                            message="direct download")
                        last_log = now
            if total and received < total:
                logger.warning("直链下载不完整：%d/%d 字节", received, total)
                return False
        logger.info("直链下载完成：%d 字节，开始解压落地", tmp.stat().st_size)
        if reporter is not None:
            if stats is not None:
                stats["done_bytes"] = stats.get("done_bytes", 0) + received
            # 解压阶段：百分比推到组件区间上沿（真实模式按累计字节折算）
            pct = (min(99, stats["done_bytes"] * 100 // grand_total)
                   if grand_total and stats else span_hi)
            reporter.update(stage="extracting", component=comp_idx,
                            components=comp_count, percent=pct,
                            message="extracting zip")
        return _install_zip_file(tmp, target, exe_rel, logger)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        logger.error("直链下载/解压失败：%s", exc)
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _install_zip_file(zip_path: Path, target: Path, exe_rel: str,
                      logger: logging.Logger) -> bool:
    """把下载好的组件 zip 落盘（复用离线安装的解压/净化链路，§8.7）。

    CFT zip 顶层即 ``chrome-*64/`` 内容 → 直接解压进 target 组件目录。
    """
    # 旧残留清理（上一轮失败可能留下不完整目录，避免标记误判）
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    paths.ensure_dir(target)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if not zf.namelist():
                logger.error("内核 zip 为空：%s", zip_path)
                return False
            _extract_safe(zf, target, logger)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        logger.error("内核 zip 解压失败（含不安全条目时报 ValueError）：%s", exc)
        return False
    (target / "INSTALLATION_COMPLETE").write_text("", encoding="utf-8")
    exe_ok = (target / exe_rel.replace("/", os.sep)).is_file()
    logger.info("内核落盘完成：%s（可执行文件 %s）",
                target, "存在" if exe_ok else "缺失")
    return exe_ok


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
