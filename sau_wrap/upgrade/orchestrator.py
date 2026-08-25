# -*- coding: utf-8 -*-
"""升级编排与回滚（实施计划 S7；设计文档 §7.4、现状文档 §7.2 六步、§15.2 自愈）。

**编排主体**：服务进程（SYSTEM 上下文）直接发起，无 UAC 弹窗；控制台是唯一
确认入口（``POST /upgrade/apply``）。

六步编排（现状文档 §7.2，任务定义 5）：

    [1/6] 停服并等待（30s 窗口）
    [2/6] robocopy 备份安装目录 → updates/backup（exit>=8 判败；失败保留旧备份）
    [3/6] 杀托盘进程
    [4/6] 静默安装 ``{installer} /SILENT /SUPPRESSMSGBOXES /NORESTART /LOG``
    [5/6] 幂等启动服务（等 30s）
    [6/6] 轮询 ``GET /status`` 校验 ``version==目标 且 service_running``

任一步失败 → 自动回滚（停服 → robocopy /E /PURGE 恢复备份 → 重启服务 →
二次校验 → ``rolled_back``）；回滚也失败 → ``failed`` + 人工救援指引
（控制台升级页展示 ``download_url`` 直链，§7.4 兜底）。

**可注入执行器**（任务定义 5）：真实环境（服务进程内 / SYSTEM）用
:func:`real_executors`；开发验证（无服务注册、无安装器）注入假执行器。
真机全链路验证留待 S8 打包步骤后（runner 副本形态见 §7.4 端到端链路）。

**启动自检三分支**（§15.2，断电/崩溃自动收敛）：见 :func:`startup_selfcheck`。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sau_wrap.version import APP_VERSION

#: 停服 / 启服等待窗口（秒，任务定义 5）
SERVICE_WAIT_SECONDS = 30.0

#: 终态校验轮询窗口（现状文档 §7.2 [6/6]：60s 窗口、3s 轮询）
VERIFY_WINDOW_SECONDS = 60.0
VERIFY_POLL_SECONDS = 3.0

#: 人工救援指引（§7.4 兜底 + §15.2 分支 3）
MANUAL_RESCUE_GUIDE = (
    "自动升级失败且无法自愈：请从升级页下载链接手动下载安装包安装，"
    "或联系管理员；详情见 service.log / upgrade.log。"
)


@dataclass
class UpgradeExecutor:
    """编排执行器接口（**可注入/可替换**：真实与假实现共用同一契约）。

    所有字段均为可调用对象；编排只做流程控制与状态推进，环境动作全部委托执行器。
    """

    #: 停服并等待退出（窗口 30s）→ 是否成功
    stop_service: Callable[[], bool]
    #: 备份安装目录 → backup_dir（robocopy，exit>=8 判败）→ 是否成功
    backup_dir: Callable[[Path, Path], bool]
    #: 杀托盘进程（不存在视为成功）→ 是否成功
    kill_tray: Callable[[], bool]
    #: 静默安装安装包 → 是否成功
    run_installer: Callable[[Path], bool]
    #: 幂等启动服务（窗口 30s）→ 是否成功
    start_service: Callable[[], bool]
    #: 从备份恢复安装目录（robocopy /E /PURGE）→ 是否成功
    restore_backup: Callable[[Path, Path], bool]
    #: 校验升级结果：``/status`` 可达（200=service_running）且版本符合
    verify: Callable[[str], bool]
    #: 安装目录推导（真实：服务注册表 ImagePath，回退 %ProgramFiles%\\SAU）
    resolve_install_dir: Callable[[], Path] = field(
        default=lambda: Path(r"C:\Program Files\SAU"))


class Orchestrator:
    """升级编排器（六步 + 回滚）。同步阻塞实现——真实环境跑在独立线程，
    避免卡住服务事件循环（``LocalApiServer`` 以 ``to_thread`` 调度）。"""

    def __init__(self, logger: logging.Logger, updater, executor: UpgradeExecutor,
                 local_api_port: int = 5409,
                 current_version: str = APP_VERSION) -> None:
        self._logger = logger
        self._updater = updater
        self._exec = executor
        self.local_api_port = local_api_port
        #: 自检分支判定的当前版本（默认运行中的 APP_VERSION；测试注入）
        self.current_version = current_version
        #: 编排单飞锁（线程级）：后台线程执行中再次 apply 立即拒绝，
        #: 与端点层「同步置 applying」共同构成双重单飞（review 修正）
        self._run_lock = threading.Lock()

    # ------------------------------------------------------------ apply（六步）

    def _precheck(self) -> str | None:
        """前置条件检查 → 错误描述；就绪返回 None。

        放宽接受 ``applying``（review 修正）：端点在触发编排前已**同步**置
        ``applying`` 持久化，故此处需兼容；非 applying 的未就绪态仍拒绝。
        """
        up = self._updater
        phase = up.state.get("phase")
        installer = up.installer_path()
        if phase not in ("ready", "snoozed", "applying"):
            return f"升级未就绪（当前 phase={phase or '无'}，需 ready/snoozed）"
        if installer is None or not installer.is_file():
            return "升级未就绪（安装包缺失）"
        return None

    def apply(self) -> dict:
        """``POST /upgrade/apply`` 触发的编排主体。返回结果快照（供端点/测试）。

        单飞：``_run_lock`` 非阻塞获取，编排进行中再次调用立即返回拒绝。
        """
        up, ex = self._updater, self._exec
        if not self._run_lock.acquire(blocking=False):
            self._logger.warning("升级编排已在进行中，拒绝重复 apply")
            return {"ok": False, "phase": up.state.get("phase"),
                    "error": "升级编排已在进行中"}
        try:
            return self._apply_locked()
        finally:
            self._run_lock.release()

    def _apply_locked(self) -> dict:
        up, ex = self._updater, self._exec
        target = up.state.get("version")
        installer = up.installer_path()
        err = self._precheck()
        if err:
            return {"ok": False, "phase": up.state.get("phase"), "error": err}
        if up.state.get("phase") != "applying":
            up.set_phase("applying", error=None)  # 幂等：端点已同步置过则不重写
        install_dir = ex.resolve_install_dir()
        backup = up.backup_dir()
        steps = (
            ("[1/6] 停服", lambda: ex.stop_service()),
            ("[2/6] 备份安装目录", lambda: ex.backup_dir(install_dir, backup)),
            ("[3/6] 杀托盘", lambda: ex.kill_tray()),
            ("[4/6] 静默安装", lambda: ex.run_installer(installer)),
            ("[5/6] 启动服务", lambda: ex.start_service()),
            ("[6/6] 版本校验", lambda: ex.verify(target)),
        )
        failed_step = ""
        for name, fn in steps:
            self._logger.info("升级编排 %s（目标版本=%s）", name, target)
            try:
                ok = bool(fn())
            except Exception as exc:
                self._logger.exception("升级编排 %s 异常", name)
                ok, failed_step = False, name
            if not ok:
                if not failed_step:
                    failed_step = name
                self._logger.error("升级编排失败于 %s → 启动自动回滚", failed_step)
                return self._rollback(target, backup, failed_step)
        # 全部成功：备份保留 24h 后由启动清理处理（§7.3）
        up.set_phase("success", error=None)
        self._logger.info("升级编排完成：版本 %s → success（备份保留 24h）", target)
        return {"ok": True, "phase": "success", "version": target}

    # ------------------------------------------------------------ 回滚

    def _rollback(self, target: str, backup: Path, failed_step: str) -> dict:
        """自动回滚：停服 → 恢复备份 → 重启服务 → 二次校验 → rolled_back；
        仍失败 → failed + 人工救援指引（§7.4）。"""
        ex = self._exec
        steps = (
            ("回滚:停服", lambda: ex.stop_service()),
            ("回滚:恢复备份", lambda: ex.restore_backup(backup,
                                                        ex.resolve_install_dir())),
            ("回滚:重启服务", lambda: ex.start_service()),
            ("回滚:二次校验", lambda: ex.verify(self.current_version)),
        )
        for name, fn in steps:
            self._logger.info(name)
            try:
                ok = bool(fn())
            except Exception:
                self._logger.exception("%s 异常", name)
                ok = False
            if not ok:
                self._updater.set_phase(
                    "failed",
                    error=f"失败于 {failed_step}，且回滚亦失败（{name}）。"
                          f"{MANUAL_RESCUE_GUIDE}")
                self._logger.error("回滚失败 → failed：%s（人工救援指引已落状态）", name)
                return {"ok": False, "phase": "failed",
                        "failed_step": failed_step, "rollback_failed_at": name}
        self._updater.set_phase(
            "rolled_back",
            error=f"升级失败于 {failed_step}，已自动回滚至 {self.current_version}")
        self._logger.warning("升级已回滚（失败步骤=%s）→ rolled_back", failed_step)
        return {"ok": False, "phase": "rolled_back", "failed_step": failed_step}

    # ------------------------------------------------------------ 启动自检（§15.2）

    def startup_selfcheck(self) -> dict | None:
        """服务启动自检三分支（断电/崩溃场景自动收敛，§15.2）。

        | 分支 | 条件 | 动作 |
        | --- | --- | --- |
        | 1 | ``applying`` 且当前版本==目标 | 补做校验 → ``success`` → 清理备份 |
        | 2 | ``applying`` 且版本不符（半替换）且备份存在 | 自动回滚 → ``rolled_back`` |
        | 3 | ``applying`` 且版本不符且无备份 | 不可自愈 → ``failed`` + 人工指引 |

        非 ``applying`` 状态不做动作（返回 None）。
        """
        up, ex = self._updater, self._exec
        if up.state.get("phase") != "applying":
            return None
        target = up.state.get("version") or ""
        backup = up.backup_dir()
        if self.current_version_matches(target):
            # 分支 1：安装已完成未收尾 → 补做校验
            ok = False
            try:
                ok = bool(ex.verify(target))
            except Exception:
                self._logger.exception("启动自检（分支1）校验异常")
            if ok:
                up.set_phase("success", error=None)
                self._cleanup_backup(backup)
                self._logger.info("启动自检分支1：安装已完成，补校验通过 → success")
                return {"branch": 1, "phase": "success"}
            self._logger.warning("启动自检分支1：版本已达标但 /status 校验未通过"
                                 "（服务可能尚未就绪），保持 applying 待下次自检")
            return {"branch": 1, "phase": "applying", "verified": False}
        if backup.exists():
            # 分支 2：半替换 → 自动回滚
            self._logger.warning("启动自检分支2：版本不符（当前=%s 目标=%s）且备份存在"
                                 " → 自动回滚", self.current_version, target)
            result = self._rollback(target, backup, "升级中断（启动自检）")
            return {"branch": 2, **result}
        # 分支 3：无备份不可自愈
        up.set_phase("failed",
                     error=f"升级中断且无备份可回滚（当前={self.current_version} "
                           f"目标={target}）。{MANUAL_RESCUE_GUIDE}")
        self._logger.error("启动自检分支3：无备份不可自愈 → failed（人工救援指引）")
        return {"branch": 3, "phase": "failed"}

    def current_version_matches(self, target: str) -> bool:
        """当前版本 == 目标版本（自检分支判定；默认取运行中的 ``APP_VERSION``）。"""
        return bool(target) and str(target).strip() == self.current_version.strip()

    def _cleanup_backup(self, backup: Path) -> None:
        if not backup.exists():
            return
        try:
            import shutil
            shutil.rmtree(backup, ignore_errors=True)
            self._logger.info("已清理升级备份：%s", backup)
        except OSError:
            pass


# ================================================================ 真实执行器


def resolve_install_dir() -> Path:
    """安装目录推导（§7.3 要点）：服务注册表 ``ImagePath`` → 回退
    ``%ProgramFiles%\\SAU``；打包态（sys.frozen）直接取 exe 所在目录。"""
    import sys

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    try:
        import winreg

        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Services\SAUAgentService") as k:
            image_path = winreg.QueryValueEx(k, "ImagePath")[0]
        exe = str(image_path).strip().strip('"').split('"')[0]
        if exe:
            return Path(exe).resolve().parent
    except OSError:
        pass
    import os
    return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "SAU"


def _run(argv: list[str], ok_exit=frozenset({0})) -> bool:
    import subprocess
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False
    return proc.returncode in ok_exit


def real_executors(logger: logging.Logger,
                   port: int = 5409, token: str = "") -> UpgradeExecutor:
    """真实执行器（SYSTEM 上下文，无 UAC；开发环境不会触发，仅供真机）。

    注：``/status`` 校验走本地令牌鉴权；安装器为 Inno Setup 产物，
    ``/SILENT /SUPPRESSMSGBOXES /NORESTART /LOG``（§7.4 步骤 4）。
    """
    import subprocess

    def stop_service() -> bool:
        try:
            import win32serviceutil
            win32serviceutil.StopService("SAUAgentService")
        except Exception as exc:
            logger.warning("停服请求失败：%s", exc)
        deadline = time.monotonic() + SERVICE_WAIT_SECONDS
        while time.monotonic() < deadline:
            try:
                import win32serviceutil
                status = win32serviceutil.QueryServiceStatus("SAUAgentService")[1]
                if status == 1:  # SERVICE_STOPPED
                    return True
            except Exception:
                return True  # 服务不存在视为已停止
            time.sleep(1.0)
        return False

    def start_service() -> bool:
        try:
            import win32serviceutil
            win32serviceutil.StartService("SAUAgentService", None)
        except Exception as exc:
            logger.warning("启服请求失败（幂等容忍）：%s", exc)
        deadline = time.monotonic() + SERVICE_WAIT_SECONDS
        while time.monotonic() < deadline:
            try:
                import win32serviceutil
                status = win32serviceutil.QueryServiceStatus("SAUAgentService")[1]
                if status == 4:  # SERVICE_RUNNING
                    return True
            except Exception:
                return False
            time.sleep(1.0)
        return False

    def backup(src: Path, dst: Path) -> bool:
        # robocopy：exit 0-7 为成功级别，>=8 判败（任务定义 5；失败保留旧备份）
        return _run(["robocopy", str(src), str(dst), "/E", "/PURGE",
                     "/R:1", "/W:1", "/NFL", "/NDL", "/NP"],
                    ok_exit=frozenset(range(8)))

    def restore(src: Path, dst: Path) -> bool:
        return _run(["robocopy", str(src), str(dst), "/E", "/PURGE",
                     "/R:1", "/W:1", "/NFL", "/NDL", "/NP"],
                    ok_exit=frozenset(range(8)))

    def kill_tray() -> bool:
        # 托盘进程命令行含 "sau.exe tray" 子命令；服务主体在 [1/6] 已停，
        # 按 CommandLine 过滤避免误杀（无匹配时 WMIC 亦返回 0，幂等容忍）
        return _run(["wmic", "process", "where",
                     "CommandLine like '%sau.exe%tray%' and Name like '%.exe'",
                     "delete"], ok_exit=frozenset({0}))

    def run_installer(installer: Path) -> bool:
        from sau_wrap import paths
        log = paths.ensure_logs_dir() / "installer.log"
        return _run([str(installer), "/SILENT", "/SUPPRESSMSGBOXES",
                     "/NORESTART", f"/LOG={log}"], ok_exit=frozenset({0, 1}))

    def verify(target: str) -> bool:
        """轮询 ``GET /status``（60s 窗口 3s 轮询）：200 且 version==目标。"""
        deadline = time.monotonic() + VERIFY_WINDOW_SECONDS
        while time.monotonic() < deadline:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/status",
                    headers={"X-SAU-Local-Token": token})
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if resp.status == 200:
                        body = json.loads(resp.read().decode("utf-8"))
                        if str(body.get("version")) == str(target):
                            return True
            except Exception:
                pass
            time.sleep(VERIFY_POLL_SECONDS)
        return False

    return UpgradeExecutor(
        stop_service=stop_service,
        backup_dir=backup,
        kill_tray=kill_tray,
        run_installer=run_installer,
        start_service=start_service,
        restore_backup=restore,
        verify=verify,
        resolve_install_dir=resolve_install_dir,
    )
