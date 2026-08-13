"""
sau_ops
~~~~~~~
SAU 统一运维 CLI 入口（打包为 sau-ops.exe）。

子命令：
- service install|uninstall|start|stop|restart|status   服务运维
- service upgrade [--installer X --target-version Y | --rollback]  升级编排
- tray                                                  前台启动服务（调试）
- bind --server <url> --token <agent-token>             绑定 opcgeo
- status                                                打印服务/连接/账号/机器绑定摘要
- machine-code                                          显示本机机器码
- accounts list|recheck|remove --platform X --account Y 账号管理
- browser install [--from <zip>]                        patchright 内核安装
- doctor                                                环境检查
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 日志（sau-ops CLI 自身）
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)


def _setup_ops_logging() -> None:
    """初始化 sau-ops 的日志：控制台 + SAU_HOME/logs/sau-ops.log（轮转）。"""
    try:
        root = logging.getLogger()
        if root.handlers:
            return  # 避免重复 handler（模块被 import 多次时）
        root.setLevel(logging.INFO)

        # 控制台（stderr，避免干扰 stdout 的结构化输出）
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.WARNING)
        ch.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(ch)

        # 文件（轮转 5MB×3）
        try:
            log_dir = SAU_HOME / "logs" if "SAU_HOME" in dir() else Path(".") / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "sau-ops.log"
            fh = RotatingFileHandler(
                str(log_file), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            root.addHandler(fh)
        except Exception:
            pass  # SAU_HOME 未初始化等极端情况，至少保留控制台
    except Exception:
        pass  # 日志初始化失败不影响 CLI 主逻辑

# ---------------------------------------------------------------------------
# 查找系统真实 Python 解释器（Nuitka 编译后 sys.executable 指向 exe 自身）
# ---------------------------------------------------------------------------
def _find_system_python() -> str | None:
    """在系统上查找可用的 Python 解释器（非当前 Nuitka exe）。

    优先级：已知安装路径（Anaconda 等）→ PATH 中的真实 python → py launcher。
    排除 Windows Store 虚假 python stub（WindowsApps 目录下的小文件）。
    返回解释器绝对路径，找不到返回 None。
    """
    current_exe = str(Path(sys.executable).resolve()).lower() if sys.executable else ""

    def _is_valid_python(path: Path) -> bool:
        """检查是否为真实 Python 解释器（排除 Windows Store stub 和自身）。"""
        if not path.is_file():
            return False
        rp = str(path.resolve()).lower()
        if rp == current_exe:
            return False
        # Windows Store stub 位于 WindowsApps 目录，通常 < 1MB
        if "windowsapps" in rp:
            return False
        return True

    # 1. 已知安装路径（优先级最高：Anaconda / 标准 Python）
    candidates = [
        Path(os.environ.get("ProgramData", "")) / "anaconda3" / "python.exe",
        Path(os.environ.get("USERPROFILE", "")) / "anaconda3" / "python.exe",
        Path(os.environ.get("USERPROFILE", "")) / "miniconda3" / "python.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python313" / "python.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python312" / "python.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python311" / "python.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python310" / "python.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python39" / "python.exe",
        Path("C:\\Python313\\python.exe"),
        Path("C:\\Python312\\python.exe"),
        Path("C:\\Python311\\python.exe"),
        Path("C:\\Python310\\python.exe"),
        Path("C:\\Python39\\python.exe"),
    ]
    # 为什么：候选路径数量多，debug 级别记录每个候选，排障时知道"扫了哪些位置"
    for p in candidates:
        logger.debug("_find_system_python: checking known candidate %s", p)
        if _is_valid_python(p):
            # 为什么：浏览器内核安装会依赖找到的 Python，info 级记录最终命中的路径便于复现
            logger.info("_find_system_python: found valid interpreter via known path: %s", p)
            return str(p)

    # 2. PATH 中查找（排除 Windows Store stub）
    for name in ("python", "python3"):
        found = shutil.which(name)
        logger.debug("_find_system_python: checking PATH for %s → %s", name, found)
        if found and _is_valid_python(Path(found)):
            resolved = str(Path(found).resolve())
            logger.info("_find_system_python: found valid interpreter via PATH(%s): %s", name, resolved)
            return resolved

    # 3. py launcher（Windows 专用）
    py_launcher = shutil.which("py")
    logger.debug("_find_system_python: checking py launcher → %s", py_launcher)
    if py_launcher:
        resolved = str(Path(py_launcher).resolve())
        logger.info("_find_system_python: found py launcher: %s", resolved)
        return resolved

    # 为什么：找不到系统 Python 意味着 patchright 在线安装将失败，需 warning 提示用户手动处理
    logger.warning("_find_system_python: no valid system Python interpreter found (will skip browser install method 2)")
    return None

# ---------------------------------------------------------------------------
# 路径修正
# ---------------------------------------------------------------------------
_PROJECT_ROOT = str(Path(__file__).resolve().parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows GBK 控制台/重定向环境下，避免特殊字符（✓ 等）导致输出崩溃
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

# 垫片（必须在 import sau_cli 之前）
from sau_tray.home_shim import SAU_HOME, apply_home_shim

apply_home_shim()

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
_LOCAL_API_URL = "http://127.0.0.1:5410"
_PLATFORM_NAMES: dict[str, str] = {
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "tencent": "视频号",
    "kuaishou": "快手",
    "bilibili": "B站",
    "baijiahao": "百家号",
    "youtube": "YouTube",
}


# ===================================================================
# 辅助函数
# ===================================================================
def _get_local_token() -> str:
    """读取 local_token.bin。"""
    from sau_agent_pkg.config import load_local_token
    return load_local_token() or ""


def _api_get(path: str) -> dict[str, Any]:
    """调用本地 API GET 接口。"""
    url = f"{_LOCAL_API_URL}{path}"
    # 为什么：本地 API 调用是 sau-ops 与服务的主要 IPC，debug 级别记录 URL 便于排障
    logger.debug("_api_get: requesting URL=%s", url)
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-SAU-Local-Token", _get_local_token())
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.URLError as e:
        # 为什么：超时/连接拒绝最常见，warning 带原因让用户知道是服务未启动还是网络问题
        reason = getattr(e, "reason", str(e))
        if isinstance(reason, TimeoutError):
            logger.warning("_api_get timeout (5s): URL=%s", url)
        else:
            logger.warning("_api_get URLError: reason=%s, URL=%s", reason, url)
        raise
    except urllib.error.HTTPError as e:
        # 为什么：HTTP 错误通常意味着 token 无效或 API 出 bug，warning 带状态码+URL
        logger.warning("_api_get HTTPError: status=%s URL=%s", e.code, url)
        raise
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 为什么：返回非 JSON 通常是代理/防火墙插了 HTML 错误页，error 级便于排障
        logger.error("_api_get response is not valid JSON: URL=%s, prefix=%s",
                     url, raw[:200])
        raise


def _api_post(path: str, data: dict | None = None) -> dict[str, Any]:
    """调用本地 API POST 接口。"""
    url = f"{_LOCAL_API_URL}{path}"
    # 为什么：POST 会触发实际动作（recheck 等），debug 记录 URL 便于排障
    logger.debug("_api_post: requesting URL=%s", url)
    body = json.dumps(data or {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        method="POST",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    req.add_header("X-SAU-Local-Token", _get_local_token())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", str(e))
        if isinstance(reason, TimeoutError):
            # 为什么：recheck 等长耗时操作超时是常见问题，明确 warning 提示
            logger.warning("_api_post timeout (10s): URL=%s", url)
        else:
            logger.warning("_api_post URLError: reason=%s, URL=%s", reason, url)
        raise
    except urllib.error.HTTPError as e:
        # 为什么：HTTP 错误（4xx/5xx）意味着参数或服务端问题，warning 带状态码
        logger.warning("_api_post HTTPError: status=%s URL=%s", e.code, url)
        raise
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("_api_post response is not valid JSON: URL=%s, prefix=%s",
                     url, raw[:200])
        raise


# ===================================================================
# 子命令实现
# ===================================================================

# -------------------------------------------------------------------
# service
# -------------------------------------------------------------------
def cmd_service(args: argparse.Namespace) -> None:
    """服务运维子命令。"""
    action = args.action
    # 为什么：每个子命令的入口 info 是运维审计的基础——知道"谁在什么时候执行了什么动作"
    extra = ""
    if action == "upgrade":
        extra = (f" rollback={getattr(args, 'rollback', False)}"
                 f" installer={getattr(args, 'installer', None)}"
                 f" target={getattr(args, 'target_version', None)}")
    logger.info("cmd_service start: action=%s%s", action, extra)

    if action == "install":
        print(f"正在安装 SAUAgentService...")
        try:
            from sau_service.service_host import install_service
            install_service()
            print("服务安装成功。")
        except Exception as e:
            # 为什么：安装失败 error 级带 trace，便于定位权限/路径问题
            logger.error("cmd_service install failed: %s", e, exc_info=True)
            print(f"安装失败: {e}", file=sys.stderr)
            sys.exit(1)

    elif action == "uninstall":
        print(f"正在卸载 SAUAgentService...")
        try:
            from sau_service.service_host import uninstall_service
            uninstall_service()
            print("服务卸载成功。")
        except Exception as e:
            logger.error("cmd_service uninstall failed: %s", e, exc_info=True)
            print(f"卸载失败: {e}", file=sys.stderr)
            sys.exit(1)

    elif action == "start":
        try:
            from sau_service.service_host import start_service
            start_service()
            print("服务已启动。")
        except Exception as e:
            logger.error("cmd_service start failed: %s", e, exc_info=True)
            print(f"启动失败: {e}", file=sys.stderr)
            sys.exit(1)

    elif action == "stop":
        try:
            from sau_service.service_host import stop_service
            stop_service()
            print("服务已停止。")
        except Exception as e:
            logger.error("cmd_service stop failed: %s", e, exc_info=True)
            print(f"停止失败: {e}", file=sys.stderr)
            sys.exit(1)

    elif action == "restart":
        try:
            from sau_service.service_host import restart_service
            restart_service()
            print("服务已重启。")
        except Exception as e:
            logger.error("cmd_service restart failed: %s", e, exc_info=True)
            print(f"重启失败: {e}", file=sys.stderr)
            sys.exit(1)

    elif action == "status":
        try:
            from sau_service.service_host import get_service_status
            status = get_service_status()
            print(f"服务状态: {status}")
        except Exception as e:
            logger.warning("cmd_service status query failed: %s", e)
            print(f"查询失败: {e}", file=sys.stderr)
            sys.exit(1)

    elif action == "upgrade":
        # 升级编排（通常由托盘提权拉起；--rollback 为手动回滚入口，
        # 应对中途崩溃/断电残留 applying 态）
        from sau_agent_pkg import upgrade_orchestrator as orch

        if getattr(args, "rollback", False):
            # 为什么：回滚是高风险动作，前后 info 配对留痕
            logger.info("cmd_service upgrade: executing rollback()")
            ok = orch.rollback()
            if not ok:
                # 为什么：回滚失败意味着版本可能处于不一致状态，error 警示
                logger.error("cmd_service upgrade rollback() returned False (failed)")
            else:
                logger.info("cmd_service upgrade rollback() succeeded")
            sys.exit(0 if ok else 1)

        installer = getattr(args, "installer", None)
        target = getattr(args, "target_version", None)
        if not installer or not target:
            # 缺省读 upgrade_state.json
            from sau_agent_pkg.updater import load_upgrade_state
            state = load_upgrade_state() or {}
            installer = installer or state.get("installer_path")
            target = target or state.get("version")
            # 为什么：记录从 upgrade_state.json 回填了什么参数，校验用户是否知道用了默认值
            logger.info("cmd_service upgrade: params missing, loaded from upgrade_state.json -> installer=%s target=%s",
                        installer, target)
        if not installer or not target:
            logger.error("cmd_service upgrade: still missing installer/target after state fallback — aborting")
            print("错误: 缺少 --installer/--target-version，"
                  "且 upgrade_state.json 中无可用记录", file=sys.stderr)
            sys.exit(1)
        # 为什么：run_upgrade 前后 info 配对；校验阶段也有 info 让用户知道进度
        logger.info("cmd_service upgrade: calling run_upgrade(installer=%s, target=%s)", installer, target)
        try:
            ok = orch.run_upgrade(Path(installer), str(target))
        except Exception as e:
            # 为什么：升级过程中崩溃/回滚失败都要 error 带 trace，便于事后复盘
            logger.error("cmd_service upgrade run_upgrade raised exception: %s", e, exc_info=True)
            raise
        if ok:
            logger.info("cmd_service upgrade: run_upgrade succeeded (target=%s)", target)
        else:
            logger.error("cmd_service upgrade: run_upgrade returned False (failed/rolled back)")
        sys.exit(0 if ok else 1)

    else:
        print(f"未知操作: {action}", file=sys.stderr)
        sys.exit(1)


# -------------------------------------------------------------------
# tray
# -------------------------------------------------------------------
def cmd_tray(args: argparse.Namespace) -> None:
    """前台启动托盘（调试模式）。"""
    # 为什么：前台启动托盘是用户手动调试行为，info 留痕确认入口
    logger.info("cmd_tray start: launching tray in foreground (debug mode)")
    from sau_tray.tray_app import run_tray
    run_tray()


# -------------------------------------------------------------------
# bind
# -------------------------------------------------------------------
def cmd_bind(args: argparse.Namespace) -> None:
    """绑定 opcgeo 账号。"""
    server_url = args.server
    token = args.token
    # 为什么：bind 是高敏感操作（写入凭证），入口 info 记录 server_url 和 token 长度（不记明文）
    logger.info("cmd_bind start: server_url=%s, token_length=%d",
                server_url, len(token) if token else 0)

    if not server_url or not token:
        logger.error("cmd_bind: missing --server or --token")
        print("错误: 必须同时指定 --server 和 --token", file=sys.stderr)
        sys.exit(1)

    print(f"绑定 opcgeo:")
    print(f"  服务器: {server_url}")
    print(f"  Token:  {token[:8]}...")

    # 直接写入配置（不经过本地 API，因为服务可能未运行）
    from sau_agent_pkg.config import load_config, save_config, save_token, get_agent_id

    # 为什么：读取现有配置前 info，确认 load_config 流程（可能因权限失败）
    logger.info("cmd_bind: reading existing config to preserve fields")
    try:
        cfg = load_config()
        cfg["server_url"] = server_url
        # 生成并持久化 agent_id（若已存在则复用）
        agent_id = get_agent_id()
        cfg["agent_id"] = agent_id
        save_config(cfg)
        save_token(token)
    except Exception as e:
        # 为什么：写入 credential 失败意味着 bind 完全没生效，error 带 trace 排障
        logger.error("cmd_bind: failed to write credentials (save_config/save_token): server_url=%s, err=%s",
                     server_url, e, exc_info=True)
        print(f"写入配置失败: {e}", file=sys.stderr)
        sys.exit(1)

    # 为什么：成功写入后 info 记录 server_url + token 长度 + agent_id 前缀（敏感字段不记录全量）
    logger.info("cmd_bind: credentials saved successfully — server_url=%s, token_length=%d, agent_id_prefix=%s...",
                server_url, len(token), (agent_id[:8] if agent_id else "(none)"))

    print(f"\n配置已保存到 {SAU_HOME}")
    print(f"Agent ID: {agent_id}")
    print("注意: token 有效期以服务端为准。")
    print("如服务正在运行，请执行 `sau-ops service restart` 或调用 /reload 接口使配置生效。")


# -------------------------------------------------------------------
# status
# -------------------------------------------------------------------
def _format_token_expire(expire_at_ms: Any) -> str:
    """将毫秒时间戳格式化为可读日期；None 表示永久。"""
    if expire_at_ms is None:
        return "永久"
    try:
        return datetime.fromtimestamp(expire_at_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(expire_at_ms)


_TOKEN_STATUS_NAMES: dict[str, str] = {
    "permanent": "永久有效",
    "normal": "正常",
    "expiring": "即将到期（≤7天）",
    "grace": "已到期（宽限期内）",
    "expired": "已过期（超宽限期）",
    "unknown": "未知",
}


def cmd_status(args: argparse.Namespace) -> None:
    """打印服务/连接/账号/机器绑定摘要。"""
    # 为什么：status 是最常用的排查入口，info 记录调用次数和时机
    logger.info("cmd_status start: collecting summary (service / ws / config / accounts)")
    from sau_agent_pkg.config import load_config, load_token
    from sau_agent_pkg.machine import get_machine_code

    cfg = load_config()
    token = load_token()
    try:
        machine_code = get_machine_code()
    except Exception as e:
        machine_code = f"采集失败（{e}）"

    print("=" * 50)
    print("  SAU Agent 状态摘要")
    print("=" * 50)
    print()

    # 服务状态
    print("[服务]")
    try:
        from sau_service.service_host import get_service_status
        svc_status = get_service_status()
        print(f"  Windows 服务: {svc_status}")
    except Exception:
        print("  Windows 服务: 查询失败")

    # 本地 API 状态
    try:
        api_status = _api_get("/status")
        print(f"  本地 API:     可达")
        print(f"  WS 连接:      {'已连接' if api_status.get('ws_connected') else '未连接'}")
        print(f"  活跃任务:     {api_status.get('active_tasks', 0)}")
        print(f"  时钟偏差:     {api_status.get('clock_offset_seconds', 0):.1f}s ({api_status.get('clock_sync_status', 'unknown')})")
        # token 有效期（服务端下发）
        token_status_key = api_status.get("token_status", "unknown")
        remaining_days = api_status.get("token_remaining_days")
        print(f"  Token 到期:   {_format_token_expire(api_status.get('token_expire_at'))}")
        if remaining_days is None:
            print(f"  Token 剩余:   永久")
        else:
            print(f"  Token 剩余:   {remaining_days:.1f} 天")
        print(f"  Token 状态:   {_TOKEN_STATUS_NAMES.get(token_status_key, token_status_key)}")
    except Exception:
        print(f"  本地 API:     不可达（服务可能未运行）")

    print()

    # 配置
    print("[配置]")
    print(f"  SAU_HOME:     {SAU_HOME}")
    print(f"  服务器:       {cfg.get('server_url', '未配置')}")
    print(f"  Agent ID:     {cfg.get('agent_id', '未配置')}")
    print(f"  Token:        {'已绑定' if token else '未绑定'}")
    print(f"  机器码:       {machine_code}")

    print()

    # 账号
    print("[账号]")
    try:
        from sau_agent_pkg import accounts
        acc_list = accounts.scan()
        if acc_list:
            for acc in acc_list:
                platform = _PLATFORM_NAMES.get(acc["platform_key"], acc["platform_key"])
                print(f"  • {platform} / {acc['account_name']}")
        else:
            print("  暂无已登录账号")
    except Exception as e:
        print(f"  查询失败: {e}")

    print()


# -------------------------------------------------------------------
# machine-code
# -------------------------------------------------------------------
def cmd_machine_code(args: argparse.Namespace) -> None:
    """显示本机机器码。"""
    # 为什么：machine-code 用于换机绑定，info 留痕便于后台审计
    logger.info("cmd_machine_code start: collecting machine code")
    from sau_agent_pkg.machine import get_machine_code
    try:
        code = get_machine_code()
    except Exception as e:
        logger.error("cmd_machine_code: collection failed: %s", e, exc_info=True)
        print(f"错误: 机器码采集失败 - {e}", file=sys.stderr)
        print()
        print("排查建议:")
        print("  1. 以管理员权限重新运行本命令")
        print("  2. 检查 WMI 服务（Winmgmt）是否正常运行: services.msc → Windows Management Instrumentation")
        print("  3. 确认 powershell 可用（wmic 在新版 Windows 上已移除，会自动回退 PowerShell）")
        sys.exit(1)
    # 为什么：机器码不是高敏感字段（仅换机绑定校验），记录完整值便于排障时核对
    logger.info("cmd_machine_code: collected code=%s", code)
    print(f"机器码: {code}")
    print()
    print("换机绑定时，请在 opcgeo 后台核对机器码是否一致。")


# -------------------------------------------------------------------
# accounts
# -------------------------------------------------------------------
def cmd_accounts(args: argparse.Namespace) -> None:
    """账号管理子命令。"""
    action = args.action
    # 为什么：账号管理（尤其 remove）是用户动作，入口 info 留痕
    extra = ""
    if action == "remove":
        extra = f" platform={getattr(args, 'platform', None)} account={getattr(args, 'account', None)}"
    logger.info("cmd_accounts start: action=%s%s", action, extra)

    if action == "list":
        from sau_agent_pkg import accounts
        acc_list = accounts.scan()
        logger.info("cmd_accounts list: scanned %d accounts", len(acc_list))
        if not acc_list:
            print("暂无已登录账号。")
            return
        print(f"已登录账号 ({len(acc_list)}):")
        for acc in acc_list:
            platform = _PLATFORM_NAMES.get(acc["platform_key"], acc["platform_key"])
            print(f"  • {platform} / {acc['account_name']}")

    elif action == "recheck":
        print("正在检查账号有效性...")
        # 为什么：recheck 会调用服务启动浏览器轮询，提交前 info 记录确认请求已发出
        logger.info("cmd_accounts recheck: POST /accounts/recheck via local API (submitted)")
        try:
            result = _api_post("/accounts/recheck")
            accounts_data = result.get("accounts", [])
            # 为什么：轮询过程 info（recheck 可能耗时较长），让用户知道进度
            logger.info("cmd_accounts recheck: server returned %d accounts, evaluating validity...", len(accounts_data))
            if not accounts_data:
                print("无账号可检查。")
                return
            valid_cnt = 0
            invalid_cnt = 0
            for acc in accounts_data:
                platform = _PLATFORM_NAMES.get(acc.get("platform_key", ""), acc.get("platform_key", ""))
                name = acc.get("account_name", "unknown")
                is_valid = bool(acc.get("is_valid"))
                if is_valid:
                    valid_cnt += 1
                else:
                    invalid_cnt += 1
                valid = "有效" if is_valid else "无效"
                print(f"  • {platform} / {name}: {valid}")
            # 为什么：完成后 info 汇总有效/失效计数，一眼看出账号健康度
            logger.info("cmd_accounts recheck completed: valid=%d invalid=%d total=%d",
                        valid_cnt, invalid_cnt, len(accounts_data))
        except Exception as e:
            logger.error("cmd_accounts recheck failed: %s", e, exc_info=True)
            print(f"检查失败: {e}", file=sys.stderr)
            print("提示: 确保服务正在运行（sau-ops service start）")
            sys.exit(1)

    elif action == "remove":
        platform = args.platform
        account = args.account
        if not platform or not account:
            print("错误: remove 需要 --platform 和 --account 参数", file=sys.stderr)
            sys.exit(1)

        cookie_file = SAU_HOME / "cookies" / f"{platform}_{account}.json"
        if cookie_file.exists():
            cookie_file.unlink()
            logger.info("cmd_accounts remove: deleted cookie file %s", cookie_file)
            print(f"已删除: {cookie_file}")
        else:
            logger.warning("cmd_accounts remove: cookie file not found: %s", cookie_file)
            print(f"账号文件不存在: {cookie_file}")

    else:
        print(f"未知操作: {action}", file=sys.stderr)
        sys.exit(1)


# -------------------------------------------------------------------
# browser
# -------------------------------------------------------------------
def _is_chromium_installed(browsers_dir: Path) -> bool:
    """检查 patchright chromium 是否已安装。"""
    if not browsers_dir.exists():
        return False
    # patchright 安装后会在 browsers_dir 下创建 chromium-* 目录
    for child in browsers_dir.iterdir():
        if child.is_dir() and child.name.startswith("chromium-"):
            return True
    return False


def _run_with_output(cmd: list[str], env: dict[str, str], label: str) -> bool:
    """执行子进程并实时输出（解决 check_call 无输出导致用户以为卡死的问题）。"""
    print(f"  [{label}] 正在下载浏览器内核（约 150MB，请耐心等待）...")
    try:
        proc = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace",
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            stripped = line.rstrip()
            if stripped:
                print(f"    {stripped}")
        proc.wait()
        if proc.returncode == 0:
            print(f"  [{label}] 安装成功")
            return True
        else:
            print(f"  [{label}] 失败 (exit code {proc.returncode})")
            return False
    except Exception as e:
        print(f"  [{label}] 异常: {e}")
        return False


def _retry_without_mirror(
    cmd: list[str], env: dict[str, str], label: str,
) -> bool:
    """下载失败后移除镜像源环境变量，用官方 CDN 重试一次。

    安装器预设 PLAYWRIGHT_DOWNLOAD_HOST 走国内镜像，但镜像可能未同步
    当前版本内核（实测 npmmirror 对 CFT 构建返回 404）。失败即回退重试：
    重试成本低且幂等（无需区分失败是否为下载源问题）。env 未设置镜像
    变量时不重试。重试仅作用于子进程环境，不改动当前进程 os.environ。
    """
    if "PLAYWRIGHT_DOWNLOAD_HOST" not in env:
        return False
    print(f"  [{label}] 镜像源下载失败，回退官方源重试...")
    retry_env = {k: v for k, v in env.items() if k != "PLAYWRIGHT_DOWNLOAD_HOST"}
    return _run_with_output(cmd, retry_env, f"{label}·官方源")


def cmd_browser(args: argparse.Namespace) -> None:
    """patchright 内核安装。"""
    action = args.action
    # 为什么：browser install 是重型操作（下载 150MB），入口 info 记录来源（zip/在线）
    extra = f" from_zip={getattr(args, 'from_zip', None)}" if action == "install" else ""
    logger.info("cmd_browser start: action=%s%s", action, extra)

    if action == "install":
        from_zip = getattr(args, "from_zip", None)
        browsers_dir = SAU_HOME / "browsers"

        if from_zip:
            # 从离线 zip 安装
            zip_path = Path(from_zip)
            if not zip_path.exists():
                logger.error("cmd_browser: zip not found: %s", zip_path)
                print(f"文件不存在: {zip_path}", file=sys.stderr)
                sys.exit(1)

            browsers_dir.mkdir(parents=True, exist_ok=True)

            # 为什么：离线解压前 info 记录，排查解压失败时知道输入是什么
            logger.info("cmd_browser: offline install — zip=%s → target=%s", zip_path, browsers_dir)
            print(f"正在从 {zip_path} 解压浏览器内核到 {browsers_dir}...")
            import zipfile
            try:
                with zipfile.ZipFile(str(zip_path), "r") as zf:
                    zf.extractall(str(browsers_dir))
                logger.info("cmd_browser: zip extract succeeded")
                print("解压完成。")
            except Exception as e:
                logger.error("cmd_browser: zip extract failed: %s", e, exc_info=True)
                print(f"解压失败: {e}", file=sys.stderr)
                sys.exit(1)
        else:
            # ── 在线安装 ──
            browsers_dir.mkdir(parents=True, exist_ok=True)
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir)

            # 快速检查：已安装则跳过
            if _is_chromium_installed(browsers_dir):
                logger.info("cmd_browser: chromium already installed, skip download (%s)", browsers_dir)
                print(f"浏览器内核已安装，跳过下载: {browsers_dir}")
                print(f"如需重新安装，请先删除 {browsers_dir} 目录。")
                return

            logger.info("cmd_browser: online install starting (browsers_dir=%s)", browsers_dir)
            print("正在安装 patchright 浏览器内核（必需组件，平台登录依赖）...")
            print(f"浏览器目录: {browsers_dir}")

            installed = False

            # 方式 1：patchright 内置 node.exe + cli.js 直接调用（Nuitka 兼容）
            try:
                from patchright._impl._driver import compute_driver_executable
                driver_executable, cli_js_path = compute_driver_executable()
                env = os.environ.copy()
                env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir)
                logger.info("cmd_browser: method 1 start (patchright built-in driver)")
                print(f"  [方式 1] driver={Path(driver_executable).name}, cli={Path(cli_js_path).name}")
                installed = _run_with_output(
                    [str(driver_executable), str(cli_js_path), "install", "chromium"],
                    env, "方式 1",
                )
                # 镜像源失败 → 移除 PLAYWRIGHT_DOWNLOAD_HOST 用官方源重试一次
                if not installed:
                    logger.info("cmd_browser: method 1 failed, retrying without mirror (official CDN)")
                    installed = _retry_without_mirror(
                        [str(driver_executable), str(cli_js_path), "install", "chromium"],
                        env, "方式 1",
                    )
            except Exception as e:
                logger.warning("cmd_browser: method 1 raised exception: %s", e)
                print(f"  [方式 1] 失败: {e}")

            # 方式 2：查找系统 Python，自动安装 patchright 后执行
            if not installed:
                logger.info("cmd_browser: method 1 did not succeed, trying method 2 (system python)")
                real_python = _find_system_python()
                if real_python:
                    print(f"  [方式 2] 使用系统 Python: {real_python}")
                    # 先确保 patchright 已安装
                    print("  [方式 2] 检查 patchright 包...")
                    pip_env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(browsers_dir)}
                    _run_with_output(
                        [real_python, "-m", "pip", "install", "patchright", "-q"],
                        pip_env, "pip",
                    )
                    installed = _run_with_output(
                        [real_python, "-m", "patchright", "install", "chromium"],
                        pip_env, "方式 2",
                    )
                    # 同方式 1：镜像源失败 → 回退官方源重试一次
                    if not installed:
                        logger.info("cmd_browser: method 2 failed, retrying without mirror")
                        installed = _retry_without_mirror(
                            [real_python, "-m", "patchright", "install", "chromium"],
                            pip_env, "方式 2",
                        )
                else:
                    logger.warning("cmd_browser: method 2 skipped (no system python found)")
                    print("  [方式 2] 未找到系统 Python 解释器", file=sys.stderr)

            # 必需组件安装失败 → 终止安装
            if not installed:
                logger.error("cmd_browser: all install methods failed (browser kernel not installed)")
                print("\n[错误] patchright 浏览器内核安装失败（必需组件）", file=sys.stderr)
                print("平台登录功能将无法使用。", file=sys.stderr)
                print("\n请尝试以下方案：", file=sys.stderr)
                print("  1. 手动安装: pip install patchright && python -m patchright install chromium", file=sys.stderr)
                print("  2. 使用离线包: sau-ops browser install --from <patchright-browsers.zip>", file=sys.stderr)
                print(f"\n浏览器目录: {browsers_dir}", file=sys.stderr)
                sys.exit(1)

            logger.info("cmd_browser: install succeeded (browsers_dir=%s)", browsers_dir)
            print(f"\n浏览器内核安装完成: {browsers_dir}")

        # 设置环境变量提示
        print(f"\n请设置环境变量: PLAYWRIGHT_BROWSERS_PATH={SAU_HOME / 'browsers'}")

    else:
        print(f"未知操作: {action}", file=sys.stderr)
        sys.exit(1)


# -------------------------------------------------------------------
# doctor
# -------------------------------------------------------------------
def cmd_doctor(args: argparse.Namespace) -> None:
    """环境检查。"""
    # 为什么：doctor 是首诊入口，info 记录执行时间/次数，便于对比修复前后差异
    logger.info("cmd_doctor start: running environment checklist (8 checks)")
    print("=" * 50)
    print("  SAU Agent 环境检查")
    print("=" * 50)
    print()

    issues: list[str] = []

    # 1. Python 版本
    py_ver = sys.version_info
    print(f"[Python] {py_ver.major}.{py_ver.minor}.{py_ver.micro}", end="")
    if py_ver.major == 3 and 10 <= py_ver.minor <= 12:
        # 为什么：每步检查 info，成功/失败都留痕（汇总时知道哪步出问题）
        logger.info("doctor[1/8] Python: ok (%s.%s.%s)", py_ver.major, py_ver.minor, py_ver.micro)
        print("  ✓")
    else:
        logger.warning("doctor[1/8] Python: version mismatch (need 3.10-3.12, got %s.%s.%s)",
                       py_ver.major, py_ver.minor, py_ver.micro)
        print("  ✗ (需要 3.10–3.12)")
        issues.append("Python 版本不在 3.10–3.12 范围内")

    # 2. SAU_HOME 完整性
    logger.info("doctor[2/8] SAU_HOME: checking required dirs at %s", SAU_HOME)
    print(f"\n[SAU_HOME] {SAU_HOME}")
    required_dirs = ["cookies", "downloads", "logs", "db", "logs/tasks"]
    for sub in required_dirs:
        p = SAU_HOME / sub
        if p.exists():
            print(f"  {sub}/  ✓")
        else:
            logger.warning("doctor[2/8] SAU_HOME: missing dir %s, attempting create", sub)
            print(f"  {sub}/  ✗ (缺失)")
            try:
                p.mkdir(parents=True, exist_ok=True)
                print(f"         → 已创建")
            except Exception as e:
                logger.error("doctor[2/8] SAU_HOME: cannot create dir %s: %s", sub, e)
                issues.append(f"无法创建目录: {p}")

    # 3. cookies 目录权限
    logger.info("doctor[3/8] cookies permission: checking write access")
    cookies_dir = SAU_HOME / "cookies"
    if cookies_dir.exists():
        try:
            test_file = cookies_dir / ".permission_test"
            test_file.write_text("test")
            test_file.unlink()
            print(f"\n[cookies 权限]  ✓ (可读写)")
        except Exception as e:
            logger.warning("doctor[3/8] cookies permission: write failed: %s", e)
            print(f"\n[cookies 权限]  ✗ ({e})")
            issues.append(f"cookies 目录不可写: {e}")

    # 4. patchright 内核
    logger.info("doctor[4/8] patchright kernel: checking installed")
    print(f"\n[patchright 内核]")
    browsers_dir = SAU_HOME / "browsers"
    pw_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if pw_path:
        print(f"  PLAYWRIGHT_BROWSERS_PATH = {pw_path}")
        if Path(pw_path).exists():
            print(f"  目录存在  ✓")
        else:
            logger.warning("doctor[4/8] patchright: PLAYWRIGHT_BROWSERS_PATH set but dir missing")
            print(f"  目录不存在  ✗")
            issues.append("PLAYWRIGHT_BROWSERS_PATH 指向的目录不存在")
    elif browsers_dir.exists() and any(browsers_dir.iterdir()):
        print(f"  已安装: {browsers_dir}  ✓")
    else:
        logger.warning("doctor[4/8] patchright: not installed (user needs to run browser install)")
        print(f"  未安装  ✗")
        print(f"  请执行: sau-ops browser install")
        issues.append("patchright 浏览器内核未安装")

    # 5. biliup 检查
    logger.info("doctor[5/8] biliup: checking import")
    print(f"\n[biliup]")
    try:
        import biliup  # type: ignore[import-untyped]
        print(f"  已安装  ✓")
    except ImportError:
        logger.warning("doctor[5/8] biliup: not installed (only affects bilibili uploads)")
        print(f"  未安装  ✗ (B 站上传需要)")
        issues.append("biliup 未安装（B 站上传需要）")

    # 6. pywin32
    logger.info("doctor[6/8] pywin32: checking import")
    print(f"\n[pywin32]")
    try:
        import win32serviceutil  # type: ignore[import-untyped]
        print(f"  已安装  ✓")
    except ImportError:
        logger.warning("doctor[6/8] pywin32: not installed (Windows service support disabled)")
        print(f"  未安装  ✗ (Windows 服务需要)")
        issues.append("pywin32 未安装（Windows 服务需要）")

    # 7. pystray / Pillow
    logger.info("doctor[7/8] pystray+Pillow: checking import")
    print(f"\n[pystray + Pillow]")
    try:
        import pystray  # type: ignore[import-untyped]
        from PIL import Image  # type: ignore[import-untyped]
        print(f"  已安装  ✓")
    except ImportError as e:
        logger.warning("doctor[7/8] pystray/Pillow missing: %s", e)
        print(f"  缺失  ✗ ({e})")
        issues.append("pystray 或 Pillow 未安装（系统服务需要）")

    # 8. 配置状态
    logger.info("doctor[8/8] config: checking server_url/agent_id/token")
    print(f"\n[配置]")
    from sau_agent_pkg.config import load_config, load_token
    cfg = load_config()
    token = load_token()
    print(f"  server_url: {cfg.get('server_url', '未配置')}")
    print(f"  agent_id:   {cfg.get('agent_id', '未配置') or '未配置'}")
    print(f"  token:      {'已绑定' if token else '未绑定'}")
    if not token:
        logger.warning("doctor[8/8] config: token not bound (agent will not connect)")
        issues.append("Token 未绑定（请执行 sau-ops bind 或在服务中绑定）")

    # 总结：warning 汇总 + error 汇总
    if issues:
        # 为什么：doctor 的最终产出，error 级汇总所有问题便于排障时一眼看到
        logger.warning("cmd_doctor done: %d issue(s) found → %s", len(issues), " | ".join(issues))
    else:
        logger.info("cmd_doctor done: all 8 checks passed (no issues)")
    print()
    print("=" * 50)
    if issues:
        print(f"  发现 {len(issues)} 个问题:")
        for i, issue in enumerate(issues, 1):
            print(f"    {i}. {issue}")
    else:
        print("  所有检查通过  ✓")
    print("=" * 50)


# ===================================================================
# argparse 构建
# ===================================================================
def build_parser() -> argparse.ArgumentParser:
    """构建命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="sau-ops",
        description="SAU Agent 统一运维 CLI",
    )
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # service
    sp_service = subparsers.add_parser("service", help="服务运维")
    sp_service.add_argument(
        "action",
        choices=["install", "uninstall", "start", "stop", "restart", "status", "upgrade"],
        help="操作",
    )
    sp_service.add_argument("--installer", help="安装包路径（upgrade 缺省时读 upgrade_state.json）")
    sp_service.add_argument("--target-version", dest="target_version",
                            help="目标版本（upgrade 缺省时读 upgrade_state.json）")
    sp_service.add_argument("--rollback", action="store_true",
                            help="手动回滚到备份版本（upgrade 子命令）")
    sp_service.set_defaults(func=cmd_service)

    # tray
    sp_tray = subparsers.add_parser("tray", help="前台启动服务（调试）")
    sp_tray.set_defaults(func=cmd_tray)

    # bind
    sp_bind = subparsers.add_parser("bind", help="绑定 opcgeo 账号")
    sp_bind.add_argument("--server", required=True, help="opcgeo 服务器 WS 地址")
    sp_bind.add_argument("--token", required=True, help="Agent Token")
    sp_bind.set_defaults(func=cmd_bind)

    # status
    sp_status = subparsers.add_parser("status", help="打印服务/连接/账号/机器绑定摘要")
    sp_status.set_defaults(func=cmd_status)

    # machine-code
    sp_mc = subparsers.add_parser("machine-code", help="显示本机机器码")
    sp_mc.set_defaults(func=cmd_machine_code)

    # accounts
    sp_accounts = subparsers.add_parser("accounts", help="账号管理")
    sp_accounts.add_argument(
        "action",
        choices=["list", "recheck", "remove"],
        help="操作",
    )
    sp_accounts.add_argument("--platform", help="平台 key（remove 时必填）")
    sp_accounts.add_argument("--account", help="账号名（remove 时必填）")
    sp_accounts.set_defaults(func=cmd_accounts)

    # browser
    sp_browser = subparsers.add_parser("browser", help="patchright 内核管理")
    sp_browser.add_argument(
        "action",
        choices=["install"],
        help="操作",
    )
    sp_browser.add_argument("--from", dest="from_zip", help="从离线 zip 安装")
    sp_browser.set_defaults(func=cmd_browser)

    # doctor
    sp_doctor = subparsers.add_parser("doctor", help="环境检查")
    sp_doctor.set_defaults(func=cmd_doctor)

    return parser


# ===================================================================
# 主入口
# ===================================================================
def main() -> None:
    """入口函数。"""
    # 为什么：进入 main 第一时间初始化 logger，否则子命令里 logger 静默
    _setup_ops_logging()
    parser = build_parser()
    args = parser.parse_args()
    # 为什么：解析完命令行立刻 info 记录命令名+主要参数，审计入口
    _cmd = getattr(args, "command", None)
    _token_len = 0
    if _cmd == "bind" and getattr(args, "token", None):
        _token_len = len(args.token)
    logger.info("main: parsed CLI — command=%s, token_len=%d, args_raw=%s",
                _cmd, _token_len, sys.argv[1:])

    if not args.command:
        parser.print_help()
        sys.exit(0)

    func = getattr(args, "func", None)
    if func:
        try:
            func(args)
        except KeyboardInterrupt:
            logger.info("main: KeyboardInterrupt (user cancelled)")
            print("\n已中断。")
            sys.exit(130)
        except Exception as e:
            logger.error("main: uncaught exception in command %s: %s", _cmd, e, exc_info=True)
            print(f"错误: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

