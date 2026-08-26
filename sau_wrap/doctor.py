# -*- coding: utf-8 -*-
"""``sau.exe doctor``：排障主入口（设计文档 §14.3 八项检查清单，实施计划 S8）。

八项（§14.3 顺序）：

1. 服务注册与运行状态（``SAUAgentService`` 存在 / RUNNING）；
2. 5409 端口占用（被占用时提示；可连则核对 ``/status``）；
3. WS 连接状态与上次断开原因（经 5409 ``/status``）；
4. Agent 凭证存在性与到期时间（``/status`` 的 token_status / token_expire_at）；
5. 浏览器内核（patchright）是否已安装（§8.7 安装位置 = browsers 目录）；
6. ``%ProgramData%\\SAU`` 可写性；
7. 磁盘剩余空间；
8. 各日志末 20 行摘要。

输出 ``[OK]/[WARN]/[FAIL]``；存在 FAIL 时退出码 1（托盘不提供启停，
doctor 是用户侧唯一排障入口，§5/§14.3）。
"""

from __future__ import annotations

import json
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import click

from sau_wrap import paths
from sau_wrap.version import APP_VERSION

_STATUS_URL_TMPL = "http://127.0.0.1:{port}/status"


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{int(n)}B"


def _local_token() -> str:
    try:
        return paths.LOCAL_TOKEN_FILE.read_bytes().decode("utf-8").strip()
    except OSError:
        return ""


def _fetch_status(port: int) -> dict | None:
    token = _local_token()
    req = urllib.request.Request(
        _STATUS_URL_TMPL.format(port=port),
        headers={"X-SAU-Local-Token": token} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError):
        return None


def _check_frozen() -> tuple[str, str]:
    """运行形态诊断（真机缺陷修复，2026-08-26）。

    Nuitka standalone 产物**不设** ``sys.frozen``，且 ``__compiled__`` 伪模块
    不进 ``sys.modules``（是模块级全局名）；冻结判定统一走
    :func:`sau_wrap.paths.is_frozen`。冻结形态的服务 ImagePath 必须指向
    sau.exe 自身（``"<exe>" agent --startup auto``，exe 取 ``sys.argv[0]``：
    Nuitka standalone 的 ``sys.executable`` 指向随附 python.exe，不可用），
    否则 pywin32 会回退查找 ``pythonservice.exe`` 导致注册失败。
    """
    frozen = paths.is_frozen()
    form = "冻结（打包）" if frozen else "源码（开发）"
    detail = (f"运行形态={form}；sys.frozen={getattr(sys, 'frozen', None)!r}；"
              f"'__compiled__' in globals()={'__compiled__' in globals()}；"
              f"sys.executable={sys.executable}；sys.argv[0]={sys.argv[0]}")
    try:
        from sau_wrap.service import ops  # noqa: PLC0415

        exe, args = ops.service_image_parts()
        detail += f"；ImagePath=\"{exe}\" {args}"
        if frozen and Path(exe).name.lower() != "sau.exe":
            return "FAIL", detail + "（冻结形态 ImagePath 必须指向 sau.exe 自身）"
    except Exception as exc:  # noqa: BLE001
        detail += f"；ImagePath 推导失败：{exc}"
        return "FAIL", detail
    return "OK", detail


def _check_service() -> tuple[str, str]:
    try:
        import win32service  # noqa: PLC0415
        import win32serviceutil  # noqa: PLC0415
    except ImportError:
        return "WARN", "pywin32 不可用（源码环境缺失？），跳过服务状态检查"
    try:
        status = win32serviceutil.QueryServiceStatus("SAUAgentService")
    except win32service.error as exc:
        if exc.winerror in (1060,):  # ERROR_SERVICE_DOES_NOT_EXIST
            return "WARN", ("服务 SAUAgentService 未注册"
                            "（首装请执行 sau service install 或重装安装包）")
        return "FAIL", f"服务查询失败：{exc}"
    state = status[1]
    if state == win32service.SERVICE_RUNNING:
        return "OK", "服务 SAUAgentService 已注册且 RUNNING"
    names = {win32service.SERVICE_STOPPED: "STOPPED",
             win32service.SERVICE_START_PENDING: "START_PENDING",
             win32service.SERVICE_STOP_PENDING: "STOP_PENDING"}
    return "WARN", f"服务 SAUAgentService 状态={names.get(state, state)}"


def _check_port_and_status(port: int) -> tuple[list, dict | None]:
    out: list = []
    bindable = True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            bindable = False
    status = None if bindable else _fetch_status(port)
    if bindable:
        out.append(("WARN", f"端口 {port} 未被占用（本地 API 未运行；"
                            "服务未启动或未绑定）"))
    elif status is not None:
        out.append(("OK", f"端口 {port} 被本地 API 占用且 /status 可达（符合预期）"))
    else:
        out.append(("FAIL", f"端口 {port} 被占用但 /status 不可达——可能被上游遗留 "
                            "sau_backend.py 或其他进程占用（§4.4）"))
    return out, status


def _check_ws(status: dict | None) -> tuple[str, str]:
    if status is None:
        return "WARN", "无法获取（本地 API 不可达，见第 2 项）"
    if status.get("ws_connected"):
        return "OK", "WS 已连接"
    reason = status.get("last_close_reason") or "-"
    suspended = "（凭证类挂起，等待 /reload 唤醒）" if status.get("suspended") else ""
    return "WARN", f"WS 未连接：上次断开原因={reason}{suspended}"


def _fmt_expire(exp) -> str:
    """到期时间安全格式化（任务 #26：time.localtime 对越界时间戳招致
    ``OSError: [Errno 22]`` 崩溃，真机 doctor 凭证检查项崩溃根因）。

    兼容三种脏值：① 服务端返回**毫秒**（>1e12 → 除 1000）；② 越界值 →
    降级显示原始数；③ 非数值 → ``-``。
    """
    if not isinstance(exp, (int, float)):
        return "-"
    val = float(exp)
    if val > 1e12:  # 毫秒时间戳归一到秒（2001-09 后秒级必 <1e12）
        val /= 1000.0
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(val))
    except (OSError, ValueError, OverflowError):
        return f"{exp}（时间戳越界，原值展示）"


def _check_credential(status: dict | None) -> tuple[str, str]:
    if not paths.CONFIG_FILE.is_file():
        return "WARN", "未绑定（config.json 不存在，先执行 sau bind）"
    if not paths.CREDENTIAL_FILE.is_file():
        return "FAIL", "config.json 存在但 credential.bin 缺失（重新 bind）"
    if status is None:
        return "WARN", "凭证文件存在；到期时间无法确认（本地 API 不可达）"
    try:
        ts = status.get("token_status", "?")
        exp_txt = _fmt_expire(status.get("token_expire_at"))
        level = "OK" if ts == "ok" else "WARN"
        return level, f"token_status={ts}；到期={exp_txt}"
    except Exception as exc:  # noqa: BLE001 诊断不因单项崩溃（任务 #26）
        return "WARN", f"凭证状态解析异常（不影响其余项）：{exc}"


def _check_browser() -> tuple[str, str]:
    from sau_wrap import browser  # noqa: PLC0415

    if browser.is_installed():
        return "OK", f"已安装：{browser.chromium_executable()}"
    return ("WARN", f"未安装：执行 sau browser install（§8.7；"
                    f"目标目录 {paths.BROWSERS_DIR}）")


def _check_writable() -> tuple[str, str]:
    try:
        paths.ensure_dir(paths.DATA_ROOT)
        probe = paths.DATA_ROOT / f".doctor_probe_{int(time.time()*1000)}"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
        return "OK", f"{paths.DATA_ROOT} 可写"
    except OSError as exc:
        return "FAIL", f"{paths.DATA_ROOT} 不可写：{exc}"


def _check_disk() -> tuple[str, str]:
    try:
        usage = shutil.disk_usage(str(paths.DATA_ROOT)[:3] or "C:\\")
    except OSError as exc:
        return "WARN", f"磁盘查询失败：{exc}"
    free = usage.free
    if free < 2 * 1024 ** 3:
        return "FAIL", (f"{paths.DATA_ROOT.drive or 'C:'} 剩余 {_fmt_bytes(free)}"
                        "（<2GB，任务素材与升级包可能写不下）")
    return "OK", f"{paths.DATA_ROOT.drive or 'C:'} 剩余 {_fmt_bytes(free)}"


def _check_ws_transport() -> tuple[str, str]:
    """WS 传输加密检查（终审修复14：ws:// 明文连接提示生产建议 wss）。"""
    from sau_wrap.agent import config as agent_config  # noqa: PLC0415

    try:
        cfg = agent_config.load_config()
    except Exception:  # noqa: BLE001
        return "WARN", "config.json 读取失败，跳过传输加密检查"
    if cfg is None:
        return "WARN", "未绑定（config.json 不存在），跳过传输加密检查"
    url = str(getattr(cfg, "server_url", "") or "")
    if url.startswith("ws://"):
        return "WARN", (f"服务端为 ws:// 明文连接（{url}），"
                        "生产环境建议改用 wss:// 加密传输")
    if url.startswith("wss://"):
        return "OK", f"服务端为 wss:// 加密连接（{url}）"
    return "WARN", f"server_url 协议不可识别：{url}"


def _check_logs() -> tuple[str, str]:
    lines: list[str] = []
    for name, f in (("service.log", paths.SERVICE_LOG_FILE),
                    ("tray.log", paths.TRAY_LOG_FILE),
                    ("upgrade.log", paths.UPGRADE_LOG_FILE)):
        if not f.is_file():
            lines.append(f"-- {name}: 不存在")
            continue
        try:
            tail = f.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
        except OSError as exc:
            lines.append(f"-- {name}: 读取失败 {exc}")
            continue
        lines.append(f"-- {name} 末 {len(tail)} 行 --")
        lines.extend(tail)
    return "OK", "\n".join(lines)


def _check_machine_code() -> tuple[str, str]:
    """机器码展示（诊断专用降级版，任务 #26）。

    CPU_ID 采集（PowerShell CIM/wmic）可能被企业终端安全软件拦截或极慢化，
    超时单项 5 秒上限（machine._cpu_id）；仍失败时降级为 MachineGuid+卷序列号。
    降级码与绑定链路完整码不同，detail 中明确标注；失败只影响本项不阻断其余。
    """
    from sau_wrap.agent import machine  # noqa: PLC0415

    try:
        code, degraded = machine.get_machine_code_or_degraded()
    except Exception as exc:  # noqa: BLE001
        return "WARN", f"机器码不可用（诊断展示，不影响服务）：{exc}"
    if degraded:
        return "WARN", (f"{code}（降级：MachineGuid+卷序列号，CPU_ID 采集失败/被拦截；"
                        "与绑定链路完整码不同）")
    return "OK", code


def run() -> int:
    """执行十一项检查（0 + ①~⑩）并打印；存在 FAIL → 退出码 1。"""
    from sau_wrap.agent import config as agent_config  # noqa: PLC0415

    port = 5409
    try:
        cfg = agent_config.load_config()
        port = int(getattr(cfg, "local_api_port", 0) or 5409)
    except Exception:
        pass

    click.echo(f"sau doctor（版本 {APP_VERSION}，数据目录 {paths.DATA_ROOT}）")
    results: list[tuple[str, str, str]] = []

    def add(title: str, level: str, detail: str) -> None:
        results.append((level, title, detail))

    # 任务 #26：单项外部调用超时防护——企业终端安全软件可能拦截/极慢化
    # 子进程（CIM/WMI/sc.exe）；超时只影响该项（WARN），不影响其余检查与退出。
    # 注意：不可用 with 上下文——Python 3.9+ __exit__ 的 shutdown(wait=True)
    # 会在超时后继续阻塞等子线程结束，使超时保护失效；不用 with，
    # 执行器引用随函数释放，线程自行收尾（不阻塞主流程）。
    import concurrent.futures  # noqa: PLC0415

    t_start = time.time()

    def guarded(title: str, fn) -> None:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            level, detail = pool.submit(fn).result(timeout=5)
        except concurrent.futures.TimeoutError:
            level, detail = "WARN", "检查超时（>5s，已跳过；疑似安全软件拦截外部调用）"
        except Exception as exc:  # noqa: BLE001
            level, detail = "WARN", f"检查异常（不影响其余项）：{exc}"
        add(title, level, detail)

    guarded("0. 运行形态", _check_frozen)
    guarded("① 服务状态", _check_service)
    port_out, status = _check_port_and_status(port)
    for lvl, detail in port_out:
        add(f"② 端口 {port}", lvl, detail)
    add("③ WS 连接", *_check_ws(status))
    add("④ WS 传输加密", *_check_ws_transport())
    add("⑤ Agent 凭证", *_check_credential(status))
    guarded("⑥ 机器码（诊断）", _check_machine_code)
    guarded("⑦ 浏览器内核", _check_browser)
    guarded("⑧ 数据目录可写", _check_writable)
    guarded("⑨ 磁盘空间", _check_disk)
    add("⑩ 日志摘要", *_check_logs())

    icon = {"OK": "[OK]  ", "WARN": "[WARN]", "FAIL": "[FAIL]"}
    has_fail = False
    for level, title, detail in results:
        has_fail = has_fail or level == "FAIL"
        click.echo(f"{icon[level]} {title}：{detail}")
    click.echo(f"总耗时 {time.time() - t_start:.1f}s（单项外部调用 5s 超时保护，任务 #26）")
    return 1 if has_fail else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
