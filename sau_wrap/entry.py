# -*- coding: utf-8 -*-
"""sau.exe 唯一入口：子命令分发（设计文档 §3.2，实施计划 S1）。

CLI 框架选型：文档 §2.4.1 建议「Typer 或 Click」，本实现采用 **Click**
（当前环境已具备，避免新增依赖；Typer 本身也构建于 Click 之上）。

子命令规格（§3.2）：

    sau agent                服务进程本体（SCM ImagePath 指向本命令）
    sau service <verb>       install|remove|start|stop|status|upgrade
    sau tray                 瘦托盘（本步占位）
    sau browser install      浏览器内核安装（本步占位）
    sau doctor               诊断（本步占位）
    sau machine-code|bind    机器码 / 绑定（S2 已实现）
    sau <平台> ...           透传上游 sau_cli.py（后续步骤实现）

本步（任务 #13 第二步 / S2）实现状态：
- agent           ✅ WS 主循环（注册/心跳/重连退避/挂起/任务落库/结果补发）
- service 五项     ✅ install/remove/start/stop/status（upgrade 占位）
- machine-code    ✅ 真实机器码（§5.7）
- bind            ✅ 写 config.json + credential.bin（DPAPI）
- 其余            ⬜ 占位提示
"""

from __future__ import annotations

import sys

import click

from sau_wrap.version import APP_VERSION


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


# ---------------------------------------------------------------- tray


@cli.command()
def tray() -> None:
    """瘦托盘（本步占位；实施计划 S5：三菜单 + Mutex + /status 轮询）。"""
    click.echo("sau tray 尚未实现（实施计划 S5，见设计文档第 5 章）。")
    click.echo("目标形态：原生菜单三项——打开控制台 / 打开日志目录 / 退出。")


# ---------------------------------------------------------------- browser


@cli.group()
def browser() -> None:
    """浏览器内核管理（§8.7 三层下载方案）。"""


@browser.command("install")
@click.option("--from-file", "from_file", type=str, default=None,
              help="离线安装：本地内核 zip 路径（§8.7 第三层兜底）")
def browser_install(from_file: str | None) -> None:
    """【占位】下载 / 安装浏览器内核（实施计划后续步骤）。"""
    if from_file:
        click.echo(f"browser install --from-file {from_file} 尚未实现（离线安装入口，§8.7）。")
    else:
        click.echo("browser install 尚未实现（镜像源下载 + 卡死换源，§8.7）。")


# ---------------------------------------------------------------- 诊断 / 绑定族


@cli.command()
def doctor() -> None:
    """【占位】诊断（实施计划后续步骤，§14.3 八项检查清单）。"""
    click.echo("sau doctor 尚未实现（八项检查清单见设计文档 §14.3）。")


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


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> None:
    """入口函数。

    特别处理：SCM 拉起时命令行带 ``--startup auto``（pywin32 服务宿主惯例），
    此时直接进入服务宿主，而不经过子命令分发（§4.1）。
    """
    if argv is None:
        argv = sys.argv[1:]
    if "--startup" in argv:
        from sau_wrap.service import host

        host.main(list(argv))
        return
    cli(args=argv)


if __name__ == "__main__":  # pragma: no cover
    main()
