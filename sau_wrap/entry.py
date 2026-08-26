# -*- coding: utf-8 -*-
"""sau.exe 唯一入口：子命令分发（设计文档 §3.2，实施计划 S1）。

CLI 框架选型：文档 §2.4.1 建议「Typer 或 Click」，本实现采用 **Click**
（当前环境已具备，避免新增依赖；Typer 本身也构建于 Click 之上）。

子命令规格（§3.2）：

    sau agent                服务进程本体（SCM ImagePath 指向本命令）
    sau service <verb>       install|remove|start|stop|status|upgrade
    sau tray                 瘦托盘（S5：三菜单 + Mutex + /status 轮询）
    sau browser install      浏览器内核安装（本步占位）
    sau doctor               诊断（本步占位）
    sau machine-code|bind    机器码 / 绑定（S2 已实现）
    sau <平台> ...           同构透传上游 sau_cli.py（终审修复⑥：§3.2/决策表行 8，
                             douyin/kuaishou/xiaohongshu/bilibili/tencent/youtube）

本步（任务 #13 第二步 / S2）实现状态：
- agent           ✅ WS 主循环（注册/心跳/重连退避/挂起/任务落库/结果补发）
- service 五项     ✅ install/remove/start/stop/status（upgrade 占位）
- machine-code    ✅ 真实机器码（§5.7）
- bind            ✅ 写 config.json + credential.bin（DPAPI）
- 其余            ⬜ 占位提示
"""

from __future__ import annotations

import os
import sys
import time

import click

from sau_wrap import paths
from sau_wrap.version import APP_VERSION

# 浏览器内核统一落 %ProgramData%\SAU\browsers（§3.6/§8.7）：
# 服务（SYSTEM）/托盘/CLI 全路径一致，避免默认 %USERPROFILE% 缓存
# 在 SYSTEM 与用户会话间不一致（任务 #19 ②-2 决策）。
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(paths.BROWSERS_DIR))


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(APP_VERSION, "--version", prog_name="sau", message="%(prog)s %(version)s")
def cli() -> None:
    """SAU 单一入口（包装层原型，任务 #12 第一步）。"""


# ---------------------------------------------------------------- agent


@cli.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True}
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def agent(args: tuple[str, ...]) -> None:
    """服务进程本体（本步为空跑骨架；WS 主循环 + 5409 API 见 S2/S3）。

    常用动词：debug（前台调试）、install、remove、start、stop；
    SCM 拉起时自动附加 ``--startup auto``。
    """
    if not args:
        click.echo("用法: sau agent <run-fg|debug|install|remove|start|stop|--startup auto>")
        click.echo("（run-fg=前台自测；其余为 pywin32 服务宿主惯例，详见设计文档 §4.1）")
        sys.exit(1)
    from sau_wrap.service import host

    host.main(list(args))


# ---------------------------------------------------------------- service


@cli.group()
def service() -> None:
    """Windows 服务生命周期管理（§4）。"""


@service.command("install")
def service_install() -> None:
    """注册服务 + 延迟自启 + 失败重启策略（需管理员）。"""
    from sau_wrap.service import ops

    ops.cmd_install()


@service.command("remove")
def service_remove() -> None:
    """停止并移除服务注册（需管理员）。"""
    from sau_wrap.service import ops

    ops.cmd_remove()


@service.command("start")
def service_start() -> None:
    """启动服务并轮询至 RUNNING（窗口 60 秒）。"""
    from sau_wrap.service import ops

    ops.cmd_start()


@service.command("stop")
def service_stop() -> None:
    """停止服务（等待至多 30 秒）。"""
    from sau_wrap.service import ops

    ops.cmd_stop()


@service.command("status")
def service_status() -> None:
    """查询服务状态与 ImagePath。"""
    from sau_wrap.service import ops

    ops.cmd_status()


@service.command("upgrade")
def service_upgrade() -> None:
    """【占位】升级编排入口（实施计划 S7，§7.4）。"""
    from sau_wrap.service import ops

    ops.cmd_upgrade()


@service.command("upgrade-run", hidden=True)
@click.option("--installer", required=True, help="安装包路径（服务侧移交）")
@click.option("--target-version", "target_version", required=True,
              help="目标版本（校验基准）")
def service_upgrade_run(installer: str, target_version: str) -> None:
    """【隐藏】升级 runner 副本执行入口（终审修复⑧，§7.4 步骤 8-10）。

    仅供服务进程升级移交时拉起 ``updates/runner/`` 副本调用，不由用户直接使用：
    停服/备份/静默安装/启服/校验/回滚全流程在本进程执行。
    """
    from sau_wrap.upgrade import runner

    result = runner.run_upgrade(installer, target_version)
    # success = 升级完成；rolled_back/failed = 系统已收敛但升级未成，退出码非 0。
    sys.exit(0 if result.get("phase") == "success" else 1)


# ---------------------------------------------------------------- tray


@cli.command()
def tray() -> None:
    """瘦托盘（S5：三菜单 + Mutex 单实例 + /status 轮询，设计文档第 5 章）。"""
    from sau_wrap.tray.app import run

    sys.exit(run())


# ---------------------------------------------------------------- browser


@cli.group()
def browser() -> None:
    """浏览器内核管理（§8.7 三层下载方案）。"""


@browser.command("install")
@click.option("--from-file", "from_file", type=str, default=None,
              help="离线安装：本地内核 zip 路径（§8.7 第三层兜底）")
def browser_install(from_file: str | None) -> None:
    """下载 / 安装浏览器内核（§8.7：镜像源 + 60s 卡死换源 + 离线兜底）。

    任务 #26（真机「无任何反应」修复）：进度日志双通道——交互终端可见 +
    同步落盘 ``%ProgramData%\\SAU\\logs\\browser_install.log``（无输出时
    可查）；启动即打印目标目录/已装判定，结束时明确成败与后续指引。
    """
    import logging

    from sau_wrap import browser

    logger = logging.getLogger("sau.browser")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    # 通道 1：交互终端（stdout；非控制台重定向场景同样被捕获）
    h_out = logging.StreamHandler(sys.stdout)
    h_out.setFormatter(fmt)
    logger.addHandler(h_out)
    # 通道 2：日志文件（无可见输出时的排障依据；失败不阻断命令本身）
    log_file = None
    try:
        log_file = paths.LOGS_DIR / "browser_install.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        h_file = logging.FileHandler(log_file, encoding="utf-8")
        h_file.setFormatter(fmt)
        logger.addHandler(h_file)
    except OSError:
        log_file = None
    try:
        click.echo(f"浏览器内核安装开始：目标目录 {browser.chromium_dir()}")
        if log_file is not None:
            click.echo(f"进度日志同步写入：{log_file}")
        if from_file:
            ok = browser.install_from_file(from_file, logger)
        else:
            ok = browser.install_online(logger)
        click.echo("浏览器内核安装成功" if ok
                   else "浏览器内核安装失败（不阻断其它功能；弱网可稍后重试，"
                        "或用离线包：sau.exe browser install --from-file <zip>）")
    finally:
        for h in (h_out,) + ((h_file,) if log_file is not None else ()):
            try:
                h.flush()
                logger.removeHandler(h)
            except Exception:  # noqa: BLE001
                pass
    if not ok:
        sys.exit(1)
    click.echo(f"浏览器内核就绪：{browser.chromium_executable()}")


# ---------------------------------------------------------------- 诊断 / 绑定族


@cli.command()
def doctor() -> None:
    """诊断（§14.3 八项检查清单；排障主入口）。"""
    from sau_wrap import doctor as doctor_mod

    sys.exit(doctor_mod.run())


@cli.command("machine-code")
def machine_code() -> None:
    """显示本机机器码（32 位十六进制，绑定链路，§5.7）。"""
    from sau_wrap.agent.machine import get_machine_code

    try:
        click.echo(get_machine_code())
    except RuntimeError as exc:
        click.echo(f"[错误] 机器码生成失败：{exc}", err=True)
        sys.exit(1)


@cli.command()
@click.option("--server", "server_url", required=True,
              help="服务端 WS 基址，如 wss://host/opcgeo/agent/ws")
@click.option("--token", "token", required=True, help="管理端创建 Agent 时的一次性 token")
@click.option("--agent-id", "agent_id", default=None,
              help="可选；缺省自动生成/沿用（32 位 UUID hex）")
def bind(server_url: str, token: str, agent_id: str | None) -> None:
    """绑定 opcgeo：写 config.json + credential.bin（DPAPI LOCAL_MACHINE）。"""
    from sau_wrap.agent import config as agent_config

    try:
        cfg = agent_config.bind(server_url, token, agent_id)
    except (ValueError, RuntimeError) as exc:
        click.echo(f"[错误] 绑定失败：{exc}", err=True)
        sys.exit(1)
    click.echo("绑定成功：")
    click.echo(f"  server_url: {cfg.server_url}")
    click.echo(f"  agent_id:   {cfg.agent_id}")
    click.echo(f"  配置:       {agent_config.paths.CONFIG_FILE}")
    click.echo(f"  凭证:       {agent_config.paths.CREDENTIAL_FILE}（DPAPI 加密）")
    click.echo("下一步: sau service start（或 sau agent run-fg 前台验证）")


# ---------------------------------------------------------------- 平台 CLI 透传（§3.2 / 决策表行 8，终审修复⑥）
#
# 与上游 sau_cli.py argparse 分组同构：``sau <platform> <action> ...`` 全参数
# 透传（含 --help，由上游 argparse 自行处理）；转发实现见 sau_wrap.cli_bridge。


def _register_platform_commands() -> None:
    from sau_wrap.cli_bridge import UPSTREAM_PLATFORMS, forward

    for platform in UPSTREAM_PLATFORMS:

        def _cmd(args: tuple[str, ...], _platform: str = platform) -> None:
            sys.exit(forward([_platform, *args]))

        _cmd.__doc__ = (
            f"平台 CLI 透传（上游 sau_cli.py，§3.2）："
            f"sau {platform} login|cookie-auth|check|upload-video|upload-note …")
        cli.command(
            platform,
            context_settings={"ignore_unknown_options": True,
                              "allow_extra_args": True,
                              "help_option_names": []},  # --help 交上游 argparse
        )(click.argument("args", nargs=-1, type=click.UNPROCESSED)(_cmd))


_register_platform_commands()


# ---------------------------------------------------------------- main


def _stream_is_console(stream) -> bool:
    """True = 流直连真实控制台/ConPTY（WriteConsoleW 语义）。

    判定用 ``isatty()``（GetConsoleMode 成功与否）而非 GetConsoleWindow：
    ConPTY 终端（Windows Terminal / PS 7）无窗口句柄但仍是控制台语义。
    """
    try:
        isatty = getattr(stream, "isatty", None)
        if isatty is None:
            return False
        return bool(isatty())
    except Exception:  # noqa: BLE001
        return False


class _ResilientTextWriter:
    """写入失败降级丢弃的输出包装（任务 #26：任何终端环境绝不因输出崩溃）。

    仅包非控制台流（重定向/管道/哑流）。write/flush 吞掉一切异常按成功计，
    其余属性（encoding/isatty/fileno/errors/reconfigure…）透传内层流。

    bytes 写入纪律（任务 #26 真机零输出根因修复）：click 的
    ``_is_binary_writer`` 用 ``write(b"")`` 探测流类型；若对 bytes 写入吞掉
    TypeError，本类会被误判为二进制流，click 再在其上包文本编码层、
    把消息编码为 bytes 写回，底层 TextIOWrapper 报 TypeError 又被吞，
    输出静默全丢（--version/--help/doctor 零输出）。故 bytes 必须真实写入：
    优先底层 ``buffer``（二进制通道），否则按内层编码解码后写入。
    """

    def __init__(self, inner):
        self._inner = inner

    def write(self, s):
        try:
            if isinstance(s, (bytes, bytearray)):
                buf = getattr(self._inner, "buffer", None)
                if buf is not None:
                    n = buf.write(s)
                    buf.flush()
                    return n
                enc = getattr(self._inner, "encoding", None) or "utf-8"
                err = getattr(self._inner, "errors", None) or "replace"
                return self._inner.write(bytes(s).decode(enc, errors=err))
            return self._inner.write(s)
        except Exception:  # noqa: BLE001 句柄失效/编码异常：降级丢弃不崩溃
            try:
                return len(s)
            except Exception:  # noqa: BLE001
                return 0

    def flush(self):
        try:
            self._inner.flush()
        except Exception:  # noqa: BLE001
            pass

    def writable(self):
        return True

    def __getattr__(self, name):
        return getattr(self._inner, name)


def harden_stdio_encoding() -> None:
    """入口级编码加固（终审追加重构，任务 #26 真机崩溃根治）。

    按流类型分路，两条历史崩溃链均被切断：
    1. GBK（CP936）管道终端输出非 CP936 字符（``²`` ``✓`` ``°`` emoji 及上游
       透传不可控输出）抛 ``UnicodeEncodeError`` → 非控制台流统一
       ``reconfigure(encoding="utf-8", errors="replace")``；
    2. **对控制台流做 reconfigure 会把 WriteConsoleW 路径降级为 WriteFile，
       控制台句柄拒绝并报 ``OSError: [Errno 22] Invalid argument``**
       （2026-08-26 真机：doctor/--version 交互零输出）→ 控制台/ConPTY 流
       （isatty() True）**一律跳过**：WriteConsoleW 原生支持全部 Unicode；
    3. 非控制台流再包 :class:`_ResilientTextWriter`：句柄失效等写入异常
       降级丢弃，命令逻辑不因输出通道崩溃。

    逐流 try/except：无控制台（Session 0）/哑流/不支持场景绝不引发新崩溃。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is None:
                continue
            if _stream_is_console(stream):
                continue  # WriteConsoleW 语义，禁 reconfigure（EINVAL 崩溃源）
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001（无控制台/重定向/不支持：不得新崩）
            continue
    try:
        if sys.stdout is not None and not _stream_is_console(sys.stdout):
            sys.stdout = _ResilientTextWriter(sys.stdout)
        if sys.stderr is not None and not _stream_is_console(sys.stderr):
            sys.stderr = _ResilientTextWriter(sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> None:
    """入口函数。

    特别处理：SCM 拉起时命令行带 ``--startup auto``（pywin32 服务宿主惯例），
    此时直接进入服务宿主，而不经过子命令分发（§4.1）。

    控制台窗口策略（§7.5 实施验证点 2）：产物以 --windows-console-mode=disable
    构建（无双击黑窗，终端调用时 stdout 继承可见）；未附控制台时的致命异常
    经 ``AllocConsole`` 兜底弹窗排障。
    """
    if argv is None:
        argv = sys.argv[1:]
    harden_stdio_encoding()  # 子命令分发前统一加固（含上游透传路径）
    try:
        if "--startup" in argv:
            from sau_wrap.service import host

            host.main(list(argv))
            return
        cli(args=argv)
    except Exception:
        _fatal_with_console_fallback()
        raise


def _crash_log_path():
    """崩溃报告落盘路径（%ProgramData%\\SAU\\logs\\last_crash.txt）。

    语义（任务 #26 复查定案）：每次崩溃**覆盖写、恒保留最近一份**——
    不会无限残留旧崩溃；历史详情以 service.log 为准。
    """
    try:
        from sau_wrap import paths  # noqa: PLC041

        p = paths.LOGS_DIR / "last_crash.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:  # noqa: BLE001
        return None


def _wait_enter_bounded(seconds: int = 60) -> None:
    """限秒等待回车（任务 #26：替代 input() 无限阻塞——控制台不可见时
    input() 会让进程永久挂起，真机 HasExited=False 的直接根因）。"""
    try:
        import msvcrt

        deadline = time.time() + seconds
        while time.time() < deadline:
            if msvcrt.kbhit() and msvcrt.getwch() == "\r":
                return
            time.sleep(0.2)
    except Exception:  # noqa: BLE001
        pass


def _fatal_with_console_fallback() -> None:
    """无可见控制台时的致命异常兜底（任务 #26 重写：可见+留痕+绝不阻塞）。

    判定注意：GUI 子系统下 Python 3.6+ 的 stdout/stderr 是非 None 哑流，
    不能以 ``is None`` 判断；用 ``GetConsoleWindow()`` 判是否已附**窗口**控制台。
    处置（旧版 ``input()`` 无限阻塞是进程挂起根因，已移除）：
    1. 崩溃全文覆盖写 ``%ProgramData%\\SAU\\logs\\last_crash.txt``（恒 1 份）；
    2. **MessageBoxTimeoutW 15 秒自动关闭**弹窗显示摘要与日志路径（任务 #26：
       旧版 MessageBoxW 无超时，管道/隐藏窗口等无人值守环境会无限阻塞——
       冻结产物 doctor 管道环境 120s 超时实测锤实）；
    3. 仅当 MessageBoxTimeoutW 不可用（极旧系统）才回退 AllocConsole +
       限秒等待回车（:func:`_wait_enter_bounded`，60s 上限）。
    """
    import traceback

    tb_text = traceback.format_exc()
    try:
        p = _crash_log_path()
        if p is not None:
            p.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')} argv={sys.argv}\n{tb_text}",
                         encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    try:
        import ctypes
        if ctypes.windll.kernel32.GetConsoleWindow():
            return  # 窗口控制台：traceback 已随异常写到调用方终端；
            # 编码已由 main() 的 harden_stdio_encoding() 加固，非 CP936 字符不崩
    except Exception:
        pass  # 判定失败则保守走兜底分支（AllocConsole 重复调用无副作用）
    brief = tb_text.strip().splitlines()
    brief = brief[-1] if brief else "未知异常"
    msg = (f"sau.exe 发生致命异常：\n{brief}\n\n"
           f"完整堆栈：%ProgramData%\\SAU\\logs\\last_crash.txt")
    try:
        import ctypes
        # MessageBoxTimeoutW：15 秒无人响应自动关闭（绝不无限阻塞，任务 #26）
        ctypes.windll.user32.MessageBoxTimeoutW(
            0, msg, "SAU 致命异常", 0x10 | 0x00010000, 0, 15000)
        return
    except Exception:  # noqa: BLE001 极旧系统无此 API：回退限秒分支
        pass
    try:
        import ctypes
        if ctypes.windll.kernel32.AllocConsole():
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
            print("sau.exe 发生致命异常：", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            print("（60 秒内按回车退出，或等待自动退出；详情见 last_crash.txt）",
                  file=sys.stderr)
            _wait_enter_bounded(60)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
