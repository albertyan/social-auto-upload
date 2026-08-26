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
    """下载 / 安装浏览器内核（§8.7：镜像源 + 60s 卡死换源 + 离线兜底）。"""
    import logging

    from sau_wrap import browser

    logger = logging.getLogger("sau.browser")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    if from_file:
        ok = browser.install_from_file(from_file, logger)
    else:
        ok = browser.install_online(logger)
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


def harden_stdio_encoding() -> None:
    """入口级编码加固（终审追加：真机 ``UnicodeEncodeError: 'gbk' codec`` 根治）。

    GBK（CP936）终端下输出**任何**非 CP936 字符（``²`` ``✓`` ``°`` emoji，
    及上游透传的不可控输出）都会抛 ``UnicodeEncodeError`` 致整条命令崩溃；
    入口统一把 stdout/stderr 强制 ``utf-8 + errors='replace'``（终端按自身解码
    呈现，最差乱码但不崩溃）。服务进程（Session 0 无控制台）与托盘哑流场景：
    逐流 try/except 兜底，无 reconfigure / 流不可写等情形绝不引发新崩溃。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is None:
                continue
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001（无控制台/重定向/不支持：不得新崩）
            continue


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


def _fatal_with_console_fallback() -> None:
    """无控制台时（disable 模式）AllocConsole 兜底，把异常打到新控制台。

    判定注意：GUI 子系统下 Python 3.6+ 的 stdout/stderr 是非 None 哑流，
    不能以 ``is None`` 判断；改用 ``GetConsoleWindow()`` 判是否已附控制台。
    """
    try:
        import ctypes
        if ctypes.windll.kernel32.GetConsoleWindow():
            return  # 终端调用：输出已在调用方终端可见，无需兜底；
            # 编码已由 main() 的 harden_stdio_encoding() 加固，非 CP936 字符不崩
    except Exception:
        pass  # 判定失败则保守走兜底分支（AllocConsole 重复调用无副作用）
    try:
        import ctypes
        if ctypes.windll.kernel32.AllocConsole():
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
            import traceback
            print("sau.exe 发生致命异常：", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            input("按回车键退出...")
    except Exception:
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
