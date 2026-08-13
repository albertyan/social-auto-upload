"""
sau_agent_pkg.upgrade_orchestrator
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
安装编排（风险核心）：停服 → 备份 → 杀托盘 → 静默安装 → 幂等启服 → 校验 → 成功清理/失败回滚。
供 sau-ops（`service upgrade`）调用，纯函数设计、可单测。

对设计文档 §9.1 的两处保守调整：
1. **先停服后备份**（设计原顺序为备份 → 停服）：避免备份运行中服务的文件句柄
   导致 robocopy 读取异常/拷贝不一致。
2. **编排进程必须从 %ProgramData%\\SAU\\updates\\runner\\sau-ops.exe 副本运行**
   （由托盘在提权前完成复制）：安装包会覆盖 {app} 目录内的 sau-ops.exe，
   若从安装目录直接运行会覆盖自身进程文件。

前置条件：
- 管理员权限（IsUserAnAdmin 自检，非管理员直接报错退出）；
- 安装包存在且 SHA-256 复核通过（对比 upgrade_state.json 中的 file_hash）。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sau_agent_pkg.config import SAU_HOME, load_local_token
from sau_agent_pkg.updater import (
    BACKUP_DIR,
    BACKUP_META_FILE,
    save_upgrade_state,
    sha256_of_file,
)
from sau_agent_pkg.version import APP_VERSION

logger = logging.getLogger(__name__)

SERVICE_NAME = "SAUAgentService"
LOCAL_API_URL = "http://127.0.0.1:5410"
INSTALL_LOG_FILE = SAU_HOME / "logs" / "install.log"

_STOP_TIMEOUT = 30          # net stop 后等待 stopped 的超时（秒）
_VERIFY_TIMEOUT = 60        # /status 版本校验窗口（秒）
_VERIFY_INTERVAL = 3        # 校验轮询间隔（秒）
_START_TIMEOUT = 30         # net start 后等待 running 的超时（秒）

_NO_WINDOW = 0x08000000     # CREATE_NO_WINDOW


def _log(msg: str) -> None:
    """同时输出到控制台与日志（sau-ops 为交互式 CLI）。"""
    print(msg)
    logger.info(msg)


# ---------------------------------------------------------------------------
# 前置工具
# ---------------------------------------------------------------------------
def is_admin() -> bool:
    """当前进程是否管理员权限。"""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def get_install_dir() -> Path:
    """解析 SAU 安装目录。

    优先从服务注册表 ImagePath 推导（sau-service.exe 所在目录），
    回退 %ProgramFiles%\\SAU。
    """
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}",
        ) as key:
            image_path, _ = winreg.QueryValueEx(key, "ImagePath")
        exe = Path(image_path.strip().strip('"'))
        if exe.parent.exists():
            return exe.parent
    except Exception:
        pass
    return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "SAU"


def _read_installed_version(install_dir: Path) -> str:
    """读取安装目录 VERSION 文件（文件系统级版本兜底），失败回退当前 APP_VERSION。"""
    try:
        return (install_dir / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return APP_VERSION


def _run(cmd: list[str], timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    """执行子进程（无控制台窗口、捕获输出）。"""
    logger.info("Exec: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        creationflags=_NO_WINDOW,
    )


def _api_get_status() -> Optional[dict[str, Any]]:
    """GET /status（每次请求重读 local_token——服务重启会重新生成 token）。"""
    try:
        req = urllib.request.Request(f"{LOCAL_API_URL}/status", method="GET")
        req.add_header("X-SAU-Local-Token", load_local_token() or "")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _get_service_status() -> str:
    from sau_service.service_host import get_service_status
    return get_service_status()


# ---------------------------------------------------------------------------
# 编排步骤
# ---------------------------------------------------------------------------
def stop_service_and_wait(timeout: int = _STOP_TIMEOUT) -> bool:
    """net stop 并轮询等待 stopped。返回是否达到 stopped。"""
    _run(["net", "stop", SERVICE_NAME])  # 已停止时报错可忽略
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = _get_service_status()
        if status == "stopped":
            return True
        time.sleep(1)
    return _get_service_status() == "stopped"


def _backup(install_dir: Path) -> bool:
    """robocopy 备份安装目录 → backup/，并写 backup_meta.json。

    先写入旁路目录 backup.new，robocopy 成功（退出码 <8）后才替换旧备份；
    失败时保留上一次成功升级的旧备份（回滚兜底不丢失），仅删除旁路目录。
    """
    staging = BACKUP_DIR.parent / (BACKUP_DIR.name + ".new")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    old_version = _read_installed_version(install_dir)
    result = _run([
        "robocopy", str(install_dir), str(staging),
        "/E", "/COPY:DAT", "/R:2", "/W:2", "/NFL", "/NDL", "/NJH",
    ])
    if result.returncode >= 8:  # robocopy 退出码 >=8 表示有失败
        _log(f"[备份] robocopy 失败 (exit={result.returncode}): {result.stderr.strip()[:500]}")
        shutil.rmtree(staging, ignore_errors=True)  # 删旁路目录，保留旧备份
        return False

    meta = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "old_version": old_version,
        "source": str(install_dir),
    }
    (staging / BACKUP_META_FILE.name).write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # 备份成功 → 替换旧备份（先删后 rename；rename 失败则保留旁路数据供人工处理）
    shutil.rmtree(BACKUP_DIR, ignore_errors=True)
    try:
        staging.rename(BACKUP_DIR)
    except OSError as e:
        _log(f"[备份] 替换旧备份目录失败: {e}（旁路目录保留: {staging}）")
        return False
    _log(f"[备份] 完成: {install_dir} → {BACKUP_DIR} (旧版本 {old_version})")
    return True


def _kill_tray() -> None:
    """taskkill 托盘进程（否则安装目录内 sau-tray.exe 被锁）。失败不阻塞。"""
    result = _run(["taskkill", "/IM", "sau-tray.exe", "/F"])
    if result.returncode == 0:
        _log("[托盘] 已终止 sau-tray.exe")
        time.sleep(1)  # 等待进程完全退出释放文件句柄
    else:
        _log("[托盘] sau-tray.exe 未在运行或终止失败（忽略）")


def _restore_tray(install_dir: Path) -> None:
    """升级成功后拉起托盘：/SILENT 安装跳过 iss 中带 skipifsilent 的托盘启动项，
    而编排步骤已 taskkill 托盘，不手动拉起则用户侧托盘消失直至重新登录。

    编排进程由托盘 runas 拉起、运行在用户会话，可直接 Popen 分离启动；
    拉起失败不影响升级成功结论。
    """
    tray_exe = install_dir / "sau-tray.exe"
    if not tray_exe.is_file():
        _log(f"[托盘] 未找到 {tray_exe}，跳过托盘恢复")
        return
    try:
        _DETACHED_PROCESS = 0x00000008
        subprocess.Popen(
            [str(tray_exe)],
            cwd=str(install_dir),
            creationflags=_DETACHED_PROCESS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        _log("[托盘] 已重新拉起 sau-tray.exe")
    except Exception as e:
        _log(f"[托盘] 拉起 sau-tray.exe 失败（不影响升级结果）: {e}")


def _run_installer(installer_path: Path) -> bool:
    """运行安装包（/SILENT 静默），等待退出码，不设超时。"""
    INSTALL_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _log(f"[安装] 运行安装包: {installer_path}")
    result = _run([
        str(installer_path),
        "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
        f"/LOG={INSTALL_LOG_FILE}",
    ])
    _log(f"[安装] 安装包退出码: {result.returncode}")
    return result.returncode == 0


def _ensure_service_running(timeout: int = _START_TIMEOUT) -> bool:
    """幂等启动：installer [Run] 段会自动 install+start 服务，
    先查状态，非 running 才 net start。"""
    status = _get_service_status()
    if status != "running":
        _log(f"[服务] 当前状态 {status}，执行 net start ...")
        _run(["net", "start", SERVICE_NAME])
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _get_service_status() == "running":
            _log("[服务] 已运行")
            return True
        time.sleep(1)
    return _get_service_status() == "running"


def _verify_status(target_version: str, timeout: int = _VERIFY_TIMEOUT,
                   interval: int = _VERIFY_INTERVAL) -> bool:
    """轮询 GET /status：version == 目标版本 且 service_running。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = _api_get_status()
        if status and status.get("service_running") and status.get("version") == target_version:
            return True
        time.sleep(interval)
    return False


def _cleanup_old_installers(keep: Path) -> None:
    """更新成功后清理 updates/ 下其他旧安装包。"""
    from sau_agent_pkg.updater import UPDATES_DIR
    if not UPDATES_DIR.exists():
        return
    for f in UPDATES_DIR.iterdir():
        try:
            if not f.is_file():
                continue
            if not (f.name.startswith("sau-") and f.name.endswith(".exe")):
                continue
            if f.samefile(keep):
                continue
            f.unlink()
            _log(f"[清理] 已删除旧安装包: {f}")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 回滚
# ---------------------------------------------------------------------------
def rollback() -> bool:
    """回滚：停服务 → robocopy backup 恢复（/E /PURGE）→ sau-service.exe install + start
    重注册 → 二次轮询 /status 确认旧版本存活 → phase=rolled_back。

    回滚失败时打 ERROR 并输出人工处理指引。
    """
    _log("=" * 60)
    _log("开始回滚 ...")

    if not BACKUP_DIR.exists() or not any(BACKUP_DIR.iterdir()):
        _log("[回滚] 错误: 备份目录不存在或为空，无法自动回滚！")
        _manual_rescue_hint()
        save_upgrade_state(phase="failed")
        return False

    # 1. 停服务（尽力而为）
    _run(["net", "stop", SERVICE_NAME])
    deadline = time.time() + 15
    while time.time() < deadline and _get_service_status() not in ("stopped",):
        time.sleep(1)

    # 1.5 终止托盘（否则 sau-tray.exe 文件锁导致 robocopy /PURGE 失败）
    _kill_tray()

    # 2. robocopy 恢复（/E /PURGE 使目标与备份完全一致；/XF 排除备份元数据）
    install_dir = get_install_dir()
    result = _run([
        "robocopy", str(BACKUP_DIR), str(install_dir),
        "/E", "/PURGE", "/R:2", "/W:2", "/XF", BACKUP_META_FILE.name,
        "/NFL", "/NDL", "/NJH",
    ])
    if result.returncode >= 8:
        _log(f"[回滚] robocopy 恢复失败 (exit={result.returncode})")
        _manual_rescue_hint()
        save_upgrade_state(phase="failed")
        return False
    _log(f"[回滚] 备份已恢复: {BACKUP_DIR} → {install_dir}")

    # 3. 重注册服务（恢复后的 sau-service.exe install）+ 启动
    sau_service_exe = install_dir / "sau-service.exe"
    if not sau_service_exe.exists():
        _log(f"[回滚] 错误: 备份中缺少 {sau_service_exe}")
        _manual_rescue_hint()
        save_upgrade_state(phase="failed")
        return False
    _run([str(sau_service_exe), "install"])
    _run(["net", "start", SERVICE_NAME])

    # 4. 二次校验：确认旧版本存活
    old_version = ""
    try:
        meta = json.loads(BACKUP_META_FILE.read_text(encoding="utf-8"))
        old_version = str(meta.get("old_version", ""))
    except (json.JSONDecodeError, OSError):
        pass

    deadline = time.time() + _VERIFY_TIMEOUT
    alive = False
    while time.time() < deadline:
        status = _api_get_status()
        if status and status.get("service_running"):
            alive = True
            if old_version and status.get("version") != old_version:
                _log(f"[回滚] 警告: 版本不匹配（期望 {old_version}，实际 {status.get('version')}）")
            break
        time.sleep(_VERIFY_INTERVAL)

    if not alive:
        _log("[回滚] 错误: 回滚后服务仍未响应 /status")
        _manual_rescue_hint()
        save_upgrade_state(phase="failed")
        return False

    save_upgrade_state(phase="rolled_back")
    _log(f"[回滚] 完成，已恢复到旧版本 {old_version or '(未知)'}")
    return True


def _manual_rescue_hint() -> None:
    hint = (
        "\n!!! 自动回滚失败，请人工处理 !!!\n"
        f"  1. 备份目录: {BACKUP_DIR}\n"
        f"  2. 手动执行: robocopy \"{BACKUP_DIR}\" \"<安装目录>\" /E /PURGE\n"
        "  3. 以管理员身份运行 <安装目录>\\sau-service.exe install\n"
        f"  4. net start {SERVICE_NAME}\n"
        f"  5. 安装日志: {INSTALL_LOG_FILE}\n"
    )
    print(hint, file=sys.stderr)
    logger.error("Manual rescue required. %s", hint)


# ---------------------------------------------------------------------------
# 主编排
# ---------------------------------------------------------------------------
def run_upgrade(installer_path: Path, target_version: str) -> bool:
    """执行完整升级编排，返回是否成功。失败时自动尝试 rollback。"""
    _log("=" * 60)
    _log(f"SAU 升级编排开始: 目标版本 {target_version}")
    _log(f"安装包: {installer_path}")
    _log("=" * 60)

    # 前置 1：管理员自检
    if not is_admin():
        _log("错误: 需要管理员权限运行（请通过托盘确认更新或提权执行）。")
        return False

    # 前置 2：安装包存在
    installer_path = Path(installer_path)
    if not installer_path.is_file():
        _log(f"错误: 安装包不存在: {installer_path}")
        save_upgrade_state(phase="failed")
        return False

    # 前置 3：SHA-256 复核（对比 upgrade_state.json 中记录的 file_hash）
    from sau_agent_pkg.updater import load_upgrade_state
    state = load_upgrade_state() or {}
    expected_hash = str(state.get("file_hash") or "").lower()
    if expected_hash:
        _log("[校验] 正在复核安装包 SHA-256 ...")
        actual_hash = sha256_of_file(installer_path)
        if actual_hash != expected_hash:
            _log(f"错误: 安装包 SHA-256 不匹配（期望 {expected_hash[:16]}...，实际 {actual_hash[:16]}...）")
            save_upgrade_state(phase="failed")
            return False
        _log("[校验] SHA-256 复核通过")
    else:
        _log("[校验] 警告: upgrade_state.json 无 file_hash，跳过复核")

    # 写 phase=applying
    save_upgrade_state(phase="applying", version=target_version,
                       installer_path=str(installer_path))

    install_dir = get_install_dir()
    _log(f"安装目录: {install_dir}")

    try:
        # 1. 停服（先停服后备份：规避运行中文件句柄问题）
        _log("[1/6] 停止服务 ...")
        if not stop_service_and_wait():
            _log(f"错误: 服务未能在 {_STOP_TIMEOUT}s 内停止（当前: {_get_service_status()}），放弃升级（不动安装）")
            save_upgrade_state(phase="failed")
            # net stop 已发出，服务可能随后真正停止且无人拉起 → 尽力恢复
            _run(["net", "start", SERVICE_NAME])
            return False
        _log("[1/6] 服务已停止")

        # 2. 备份
        _log("[2/6] 备份当前版本 ...")
        if not _backup(install_dir):
            _log("错误: 备份失败，放弃升级（不动安装）")
            save_upgrade_state(phase="failed")
            # 尽力恢复服务
            _run(["net", "start", SERVICE_NAME])
            return False

        # 3. 终止托盘
        _log("[3/6] 终止托盘进程 ...")
        _kill_tray()

        # 4. 运行安装包
        _log("[4/6] 运行安装包（静默）...")
        if not _run_installer(installer_path):
            _log("错误: 安装包执行失败，开始回滚 ...")
            return rollback()

        # 5. 幂等启动服务
        _log("[5/6] 确认服务运行 ...")
        if not _ensure_service_running():
            _log(f"错误: 服务未能在 {_START_TIMEOUT}s 内运行，开始回滚 ...")
            return rollback()

        # 6. 校验新版本
        _log(f"[6/6] 校验 /status（{_VERIFY_TIMEOUT}s 窗口，期望版本 {target_version}）...")
        if not _verify_status(target_version):
            _log("错误: 新版本校验失败（/status 无响应或版本不匹配），开始回滚 ...")
            return rollback()

        save_upgrade_state(phase="success")
        _restore_tray(install_dir)
        _cleanup_old_installers(installer_path)
        _log("=" * 60)
        _log(f"升级成功: {APP_VERSION} → {target_version}（备份保留 24h: {BACKUP_DIR}）")
        _log("=" * 60)
        return True

    except Exception:
        logger.exception("Upgrade orchestration crashed")
        _log("错误: 编排过程异常，开始回滚 ...")
        return rollback()
