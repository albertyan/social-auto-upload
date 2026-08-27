# -*- coding: utf-8 -*-
"""有头登录拉起器（任务 #4 阶段二 + 任务 #8 CreateProcessAsUser 升级）。

背景：服务（``sau.exe agent``）运行在 Session 0（无桌面、无交互窗口站），
有头浏览器（``headless=False``）必须由**用户桌面会话内的独立进程**承载。

首选路径（任务 #8）：
    ``WTSQueryUserToken`` → ``DuplicateTokenEx`` → ``CreateEnvironmentBlock``
    → ``CreateProcessAsUserW``（``lpDesktop = "winsta0\\default"``），
    直接把子进程投送到活跃用户桌面会话——不经 schtasks，无延迟、无 ACL 风险。

降级路径（CPAU 失败时回退）：
    经系统计划任务（``schtasks``，``LogonType=InteractiveToken``）把子进程投送
    到当前活跃用户桌面会话执行（兼容旧版 Windows / 异常环境）。

    服务侧（Session 0）                用户桌面会话
    ──────────────────                 ────────────────────────
    launch_headed()
      ├─ _launch_as_user() ──────────→ 有头浏览器扫码登录  （首选）
      └─ 失败 → schtasks /Create/Run → 有头浏览器扫码登录  （降级）
    POST /login/headed/result ←──────── 令牌现读 + urllib 回报（3 次重试）

纪律：
- 服务主循环为 Selector（§5.1 定案，不支持 ``create_subprocess_*``）——本模块
  全部进程操作使用 ``subprocess`` 同步调用（调用方经 ``asyncio.to_thread``
  下沉线程），并统一 ``CREATE_NO_WINDOW``（Session 0 无控制台，防弹窗/挂起）；
- 上游文件零修改（铁律）：子进程复用上游 ``*_setup``，仅 ``headless`` 差异。
"""

from __future__ import annotations

import ctypes
import logging
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from xml.sax.saxutils import quoteattr

from sau_wrap import paths

logger = logging.getLogger("sau.headed_launcher")

#: 计划任务名前缀（任务族命名空间 ``SAU\``，会话 id 后缀区分实例）
TASK_NAME_PREFIX = "SAU\\HeadedLogin-"

#: session_id 白名单（与 ``secrets.token_urlsafe`` 输出字符集一致；
#: 参与任务名/文件路径拼接，非法即拒绝，防注入与路径穿越）
_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: 任务 XML 落盘目录（%ProgramData%\SAU\headed-login\，随 DATA_ROOT 可测试隔离）
_HEADED_LOGIN_DIR_NAME = "headed-login"

#: 拉起成功后延迟删除任务注册的秒数（> 会话 300s 硬超时 + 回报重试窗口；
#: 删除只注销任务定义，不影响已在运行的进程实例）
_DEFER_DELETE_DELAY = 420.0

#: schtasks / quser 单次调用超时（秒；系统工具，防异常环境挂起启动序列）
_SUBPROCESS_TIMEOUT = 30

#: CREATE_NO_WINDOW（0x08000000）：Session 0 无控制台，防子进程因无控制台挂起
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

#: 任务 XML 目录 ACL 是否已加固（进程内一次性；幂等，失败不置位可重试）
_DIR_ACL_HARDENED = False


def _headed_login_dir() -> Path:
    """任务 XML 工作目录（随 ``SAU_DATA_ROOT`` 环境变量隔离，测试友好）。"""
    d = paths.ensure_dir(paths.DATA_ROOT / _HEADED_LOGIN_DIR_NAME)
    _harden_headed_login_dir(d)
    return d


def _harden_headed_login_dir(dir_path: Path) -> None:
    """收敛任务 XML 目录继承权限（评审修复 #2；提权纵深防御）。

    ``%ProgramData%`` 默认对 Authenticated Users 继承 Modify，标准用户可在任务
    XML 落盘到 ``schtasks /Create /XML`` 读取的窗口内篡改文件：SYSTEM 读注册 →
    交互用户会话执行 → 提权。此处去继承并仅授 SYSTEM/管理员完全控制。

    服务以 SYSTEM 运行有权执行 ``icacls``；失败仅告警不阻断（尽力而为加固）。
    对已存在目录幂等（重复执行结果一致）；成功后进程内置位避免重复调用。
    """
    global _DIR_ACL_HARDENED
    if _DIR_ACL_HARDENED:
        return
    try:
        proc = _run_hidden(
            ["icacls", str(dir_path), "/inheritance:r",
             "/grant:r", "SYSTEM:F", "Administrators:F"])
        if proc.returncode != 0:
            logger.warning("headed-login 目录 ACL 加固失败（不阻断）: rc=%s out=%s",
                           proc.returncode,
                           _decode_out(proc.stderr) or _decode_out(proc.stdout))
            return
        _DIR_ACL_HARDENED = True
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("headed-login 目录 ACL 加固异常（不阻断）: %s", exc)


def _run_hidden(argv: list[str], timeout: float = _SUBPROCESS_TIMEOUT
                ) -> subprocess.CompletedProcess:
    """隐藏窗口跑系统命令（统一超时 + CREATE_NO_WINDOW + 捕获输出）。"""
    return subprocess.run(
        argv, capture_output=True, timeout=timeout,
        creationflags=_CREATE_NO_WINDOW)


def _decode_out(data: bytes | None) -> str:
    """系统命令输出解码（系统首选代码页，失败退 utf-8/replace——绝不抛异常）。"""
    if not data:
        return ""
    try:
        import locale
        return data.decode(locale.getpreferredencoding(False), errors="replace")
    except (LookupError, ValueError):
        return data.decode("utf-8", errors="replace")


# ================================================================ Win32 API 声明

#: 无活跃控制台会话标记
_NO_ACTIVE_SESSION = 0xFFFFFFFF

# -- kernel32 --
_kernel32 = ctypes.windll.kernel32
_kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
_kernel32.WTSGetActiveConsoleSessionId.argtypes = []
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.GetLastError.restype = wintypes.DWORD
_kernel32.GetLastError.argtypes = []

# -- wtsapi32 --
_wtsapi32 = ctypes.windll.wtsapi32
_wtsapi32.WTSQueryUserToken.restype = wintypes.BOOL
_wtsapi32.WTSQueryUserToken.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_wtsapi32.WTSQuerySessionInformationW.restype = wintypes.BOOL
_wtsapi32.WTSQuerySessionInformationW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
    ctypes.POINTER(ctypes.c_wchar_p), ctypes.POINTER(wintypes.DWORD),
]
_wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]
_wtsapi32.WTSFreeMemory.restype = None

# -- advapi32 --
_advapi32 = ctypes.windll.advapi32
_advapi32.DuplicateTokenEx.restype = wintypes.BOOL
_advapi32.DuplicateTokenEx.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
    ctypes.c_int, ctypes.c_int, ctypes.POINTER(wintypes.HANDLE),
]
_advapi32.CreateProcessAsUserW.restype = wintypes.BOOL
_advapi32.CreateProcessAsUserW.argtypes = [
    wintypes.HANDLE, ctypes.c_wchar_p, ctypes.c_wchar_p,
    ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
    wintypes.DWORD, ctypes.c_void_p, ctypes.c_wchar_p,
    ctypes.c_void_p, ctypes.c_void_p,
]

# -- userenv --
_userenv = ctypes.windll.userenv
_userenv.CreateEnvironmentBlock.restype = wintypes.BOOL
_userenv.CreateEnvironmentBlock.argtypes = [
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.BOOL,
]
_userenv.DestroyEnvironmentBlock.restype = wintypes.BOOL
_userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]


#: SECURITY_ATTRIBUTES 结构体（CreateProcessAsUser 须传非 NULL）
class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


#: STARTUPINFOW 结构体
class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


#: PROCESS_INFORMATION 结构体
class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


# -- Win32 常量 --
_TOKEN_ALL_ACCESS = 0xF01FF
_TokenPrimary = 1
_SecurityImpersonation = 2
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_STARTF_USESHOWWINDOW = 0x00000001
_SW_SHOW = 1


# ================================================================ 活跃会话探测


def find_interactive_user() -> tuple[str, int] | None:
    """探测当前是否存在活跃桌面会话，返回 ``(username, session_id)`` 或 ``None``。

    经 ``kernel32.WTSGetActiveConsoleSessionId`` 取活跃控制台会话
    （``0xFFFFFFFF`` 表示无活跃会话），再经 ``wtsapi32.WTSQuerySessionInformationW``
    读该会话的 ``WTSUserName``；空用户名视为无人登录返回 ``None``。

    .. versionchanged:: 任务 #8
       返回值由 ``str | None`` 改为 ``tuple[str, int] | None``，
       新增 session_id 供 :func:`_launch_as_user` 的 ``WTSQueryUserToken`` 使用。
       调用方（``local_api.py``）仅做 ``if not user`` 真值判断，向后兼容。
    """
    _WTS_USERNAME = 5  # WTS_INFO_CLASS.WTSUserName
    try:
        session_id = _kernel32.WTSGetActiveConsoleSessionId()
    except (AttributeError, OSError, ValueError):
        return None
    if session_id == _NO_ACTIVE_SESSION:  # 无活跃控制台会话（锁屏/无人登录）
        return None
    try:
        buffer = ctypes.c_wchar_p()
        returned = wintypes.DWORD(0)
        ok = _wtsapi32.WTSQuerySessionInformationW(
            None,  # WTS_CURRENT_SERVER_HANDLE（本机）
            session_id, _WTS_USERNAME,
            ctypes.byref(buffer), ctypes.byref(returned))
        if not ok:
            return None
        try:
            user = (buffer.value or "").strip()
        finally:
            try:
                _wtsapi32.WTSFreeMemory(buffer)
            except (AttributeError, OSError, ValueError):
                pass
    except (AttributeError, OSError, ValueError):
        return None
    if not user:
        return None
    return (user, session_id)


# ================================================================ 命令形态推导


def _command_parts() -> tuple[str, list[str]]:
    """子进程命令行推导（参照升级编排 ``spawn_runner`` 双形态，§7.4）。

    返回 ``(Command, argv 全量)``：
    - 冻结形态（安装目录 ``sau.exe``，Nuitka standalone 目录形态）；
    - 源码形态：``{sys.executable} sau_wrap\\__main__.py``（``__main__`` 自带
      sys.path/工作目录引导，SCM/计划任务工作目录不可控亦可用）。
    """
    if paths.is_frozen():
        from sau_wrap.upgrade.orchestrator import resolve_install_dir  # noqa: PLC0415

        exe = resolve_install_dir() / "sau.exe"
        return str(exe), [str(exe)]
    entry = Path(__file__).resolve().parents[1] / "__main__.py"
    return sys.executable, [sys.executable, str(entry)]


def _login_headed_args(platform: str, account: str, session_id: str,
                       channel: str | None = None,
                       user_data_dir: str | None = None) -> list[str]:
    """``login-headed`` 子命令参数段（与 entry.py 选项同名同序）。"""
    args = ["login-headed",
            "--platform", platform,
            "--account", account,
            "--session-id", session_id]
    if channel:
        args += ["--channel", channel]
    if user_data_dir:
        args += ["--user-data-dir", user_data_dir]
    return args


def build_manual_command(platform: str, account: str, session_id: str) -> str:
    """手动降级命令字符串（拉起失败时置 ``failed`` 的 message 引导文案用）。

    冻结/源码两种形态均生成可直接复制执行的完整命令行（路径带引号）。
    """
    _, argv = _command_parts()
    parts = [quoteattr(p)[1:-1] if " " in p else p for p in argv]
    return " ".join(parts + _login_headed_args(platform, account, session_id))


# ================================================================ 任务 XML


def _task_xml(command: str, arguments: str) -> str:
    """计划任务 XML（``LogonType=InteractiveToken``：以当前交互用户身份运行）。

    无 ``UserId`` + InteractiveToken 语义即「投放到活跃桌面会话」；
    ``ExecutionTimeLimit`` PT15M 兜底防僵尸任务（子进程自身 300s 硬超时 +
    回报窗口，此处取更宽的系统级上限）。内容与参数均白名单校验过，
    属性值经 ``quoteattr`` 转义，XML 注入面已闭合。
    """
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\r\n'
        '<Task version="1.2" '
        'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\r\n'
        '  <RegistrationInfo>\r\n'
        '    <Author>SAU Agent</Author>\r\n'
        '    <Description>SAU headed login (interactive desktop session)</Description>\r\n'
        '  </RegistrationInfo>\r\n'
        '  <Triggers />\r\n'
        '  <Principals>\r\n'
        '    <Principal id="Author">\r\n'
        '      <LogonType>InteractiveToken</LogonType>\r\n'
        '      <RunLevel>LeastPrivilege</RunLevel>\r\n'
        '    </Principal>\r\n'
        '  </Principals>\r\n'
        '  <Settings>\r\n'
        '    <MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>\r\n'
        '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\r\n'
        '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\r\n'
        '    <AllowHardTerminate>true</AllowHardTerminate>\r\n'
        '    <StartWhenAvailable>false</StartWhenAvailable>\r\n'
        '    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\r\n'
        '    <AllowStartOnDemand>true</AllowStartOnDemand>\r\n'
        '    <Enabled>true</Enabled>\r\n'
        '    <Hidden>false</Hidden>\r\n'
        '    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>\r\n'
        '    <Priority>7</Priority>\r\n'
        '  </Settings>\r\n'
        f'  <Actions Context="Author">\r\n'
        f'    <Exec>\r\n'
        f'      <Command>{quoteattr(command)[1:-1]}</Command>\r\n'
        f'      <Arguments>{quoteattr(arguments)[1:-1]}</Arguments>\r\n'
        f'      <WorkingDirectory>'
        f'{quoteattr(str(paths.DATA_ROOT))[1:-1]}</WorkingDirectory>\r\n'
        f'    </Exec>\r\n'
        f'  </Actions>\r\n'
        f'</Task>\r\n'
    )


# ================================================================ CreateProcessAsUser 投放


def _launch_as_user(cmd_args: list[str], session_id: int
                    ) -> subprocess.Popen | None:
    """经 ``CreateProcessAsUserW`` 把子进程投放到指定会话的用户桌面。

    调用链：``WTSQueryUserToken(sid)`` → ``DuplicateTokenEx`` →
    ``CreateEnvironmentBlock`` → ``CreateProcessAsUserW``（``lpDesktop``
    固定 ``winsta0\\default``）。

    成功返回 ``Popen``-like 对象（仅保证 ``.pid`` 可用）；任何环节失败返回
    ``None`` 并记日志（调用方据此降级到 schtasks）。

    所有 Win32 句均在 finally 块中释放，环境块亦销毁——无论成败。
    """
    h_token = wintypes.HANDLE()
    h_dup_token = wintypes.HANDLE()
    env_block = ctypes.c_void_p()

    # 1. WTSQueryUserToken —— 取指定会话的用户主令牌
    if not _wtsapi32.WTSQueryUserToken(session_id, ctypes.byref(h_token)):
        err = _kernel32.GetLastError()
        logger.warning("WTSQueryUserToken 失败 session=%d err=%d", session_id, err)
        return None

    try:
        # 2. DuplicateTokenEx —— 复制为 Primary Token（CreateProcessAsUser 须）
        sa = _SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(_SECURITY_ATTRIBUTES)
        sa.bInheritHandle = False
        if not _advapi32.DuplicateTokenEx(
                h_token, _TOKEN_ALL_ACCESS, ctypes.byref(sa),
                _SecurityImpersonation, _TokenPrimary,
                ctypes.byref(h_dup_token)):
            err = _kernel32.GetLastError()
            logger.warning("DuplicateTokenEx 失败 err=%d", err)
            return None

        try:
            # 3. CreateEnvironmentBlock —— 取用户环境变量块
            if not _userenv.CreateEnvironmentBlock(
                    ctypes.byref(env_block), h_dup_token, False):
                err = _kernel32.GetLastError()
                logger.warning("CreateEnvironmentBlock 失败 err=%d", err)
                return None

            try:
                # 4. 构造命令行字符串（CreateProcessAsUserW 须可写缓冲区）
                cmd_line = " ".join(
                    f'"{a}"' if " " in a else a for a in cmd_args)

                si = _STARTUPINFOW()
                si.cb = ctypes.sizeof(_STARTUPINFOW)
                si.lpDesktop = "winsta0\\default"
                si.dwFlags = _STARTF_USESHOWWINDOW
                si.wShowWindow = _SW_SHOW

                pi = _PROCESS_INFORMATION()

                # 5. CreateProcessAsUserW
                work_dir = str(paths.DATA_ROOT)
                ok = _advapi32.CreateProcessAsUserW(
                    h_dup_token,           # hToken
                    None,                   # lpApplicationName
                    cmd_line,               # lpCommandLine（可写）
                    None,                   # lpProcessAttributes
                    None,                   # lpThreadAttributes
                    False,                  # bInheritHandles
                    _CREATE_UNICODE_ENVIRONMENT,  # dwCreationFlags
                    env_block,              # lpEnvironment
                    work_dir,               # lpCurrentDirectory
                    ctypes.byref(si),       # lpStartupInfo
                    ctypes.byref(pi),       # lpProcessInformation
                )
                if not ok:
                    err = _kernel32.GetLastError()
                    logger.warning("CreateProcessAsUserW 失败 err=%d cmd=%s",
                                   err, cmd_line)
                    return None

                pid = pi.dwProcessId
                logger.info("CreateProcessAsUser 成功 pid=%d session=%d",
                            pid, session_id)

                # 关闭子进程线程句柄（不需要），保留进程句柄用于 Popen
                _kernel32.CloseHandle(pi.hThread)

                # 构造轻量 Popen-like 对象（仅 pid 被下游使用）
                popen = subprocess.Popen.__new__(subprocess.Popen)
                popen.pid = pid
                popen.returncode = None
                popen._handle = pi.hProcess  # 保留句柄供后续 wait/kill
                return popen

            except Exception:
                # CreateProcessAsUserW 之后的异常——须关闭已创建的进程/线程句柄
                try:
                    if pi.hProcess:
                        _kernel32.CloseHandle(pi.hProcess)
                    if pi.hThread:
                        _kernel32.CloseHandle(pi.hThread)
                except Exception:
                    pass
                raise

            finally:
                # 环境块始终销毁
                try:
                    _userenv.DestroyEnvironmentBlock(env_block)
                except (AttributeError, OSError, ValueError):
                    pass

        finally:
            try:
                _kernel32.CloseHandle(h_dup_token)
            except (AttributeError, OSError, ValueError):
                pass

    finally:
        try:
            _kernel32.CloseHandle(h_token)
        except (AttributeError, OSError, ValueError):
            pass


# ================================================================ 拉起主流程


def _schtasks_delete(task_name: str) -> None:
    """删除任务注册（幂等：任务不存在时非零码静默容忍）。"""
    try:
        _run_hidden(["schtasks", "/Delete", "/TN", task_name, "/F"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("计划任务删除异常（不影响主流程）: %s task=%s", exc, task_name)


def _schedule_task_delete(task_name: str, delay: float = _DEFER_DELETE_DELAY
                          ) -> None:
    """延迟异步删除任务注册（守护线程，不阻塞调用方；服务停机未执行完时
    残留由下次启动 :func:`cleanup_stale` 收敛——任务注册本身无运行副作用）。"""
    def _worker() -> None:
        time.sleep(delay)
        _schtasks_delete(task_name)

    threading.Thread(target=_worker, name=f"sau-headed-del-{task_name}",
                     daemon=True).start()


def launch_headed(platform: str, account: str, session_id: str) -> tuple[bool, str]:
    """把有头登录子进程投放到用户桌面会话（首选 CPAU，降级 schtasks）。

    返回 ``(成功与否, 手动命令字符串)``——拉起失败**不拒绝创建会话**
    （任务 #4 契约）：执行器据失败置 ``failed`` 时 message 附手动命令引导。

    投放策略（任务 #8）：
    1. 首选 ``CreateProcessAsUser`` 链——直接、无 schtasks 依赖、兼容性好；
    2. CPAU 失败（无活跃会话 / API 报错）→ 降级到 ``schtasks`` 计划任务；
    3. 两者均败 → 返回手动命令供用户自行执行。
    """
    manual_cmd = build_manual_command(platform, account, session_id)
    if not _SAFE_SESSION_ID_RE.fullmatch(session_id or ""):
        logger.error("session_id 非法，拒绝构造计划任务名: %r", session_id)
        return False, manual_cmd
    # 纵深防御（评审修复 #3）：函数内复校验平台白名单与账号净化——
    # 调用方已把关，此处双保险，非法即拒绝拉起返回失败。
    from sau_wrap.service import login_sessions as _ls  # noqa: PLC0415

    if platform not in _ls.SUPPORTED_PLATFORMS:
        logger.error("platform 非法（非支持平台），拒绝拉起: %r", platform)
        return False, manual_cmd
    if _ls.sanitize_fs_name(account) is None:
        logger.error("account 非法（未过白名单），拒绝拉起: %r", account)
        return False, manual_cmd

    _, argv = _command_parts()
    sub_args = _login_headed_args(platform, account, session_id)
    cmd_args = argv + sub_args

    # 参数校验（评审修复 #3）：拒绝含双引号的参数（白名单本已排除）
    tokens = argv[1:] + sub_args
    if any('"' in a for a in tokens):
        logger.error("参数含双引号，拒绝构造命令行: %r", tokens)
        return False, manual_cmd

    # ---- 首选路径：CreateProcessAsUser（任务 #8）----
    user_info = find_interactive_user()
    if user_info is not None:
        _, win_session_id = user_info
        try:
            popen = _launch_as_user(cmd_args, win_session_id)
            if popen is not None:
                logger.info("有头登录子进程已投放（CPAU）: pid=%d session=%d",
                            popen.pid, win_session_id)
                return True, manual_cmd
        except Exception as exc:
            logger.warning("CPAU 投放异常，降级到 schtasks: %s", exc)
    else:
        logger.info("无活跃用户会话，跳过 CPAU，尝试 schtasks 降级路径")

    # ---- 降级路径：schtasks 计划任务 ----
    return _launch_via_schtasks(cmd_args, session_id, manual_cmd)


def _launch_via_schtasks(cmd_args: list[str], session_id: str,
                         manual_cmd: str) -> tuple[bool, str]:
    """经 schtasks 计划任务投放子进程（CPAU 失败时的降级路径）。"""
    task_name = TASK_NAME_PREFIX + session_id
    command = cmd_args[0]
    tokens = cmd_args[1:]
    arguments = " ".join(f'"{a}"' for a in tokens)
    xml_file = _headed_login_dir() / f"{session_id}.xml"
    try:
        xml_file.write_bytes(
            _task_xml(command, arguments).encode("utf-16"))
    except OSError as exc:
        logger.error("任务 XML 写入失败: %s file=%s", exc, xml_file)
        return False, manual_cmd
    try:
        proc = _run_hidden(["schtasks", "/Create", "/TN", task_name,
                            "/XML", str(xml_file), "/F"])
        if proc.returncode != 0:
            logger.error("schtasks /Create 失败: rc=%s out=%s",
                         proc.returncode, _decode_out(proc.stderr)
                         or _decode_out(proc.stdout))
            return False, manual_cmd
        proc = _run_hidden(["schtasks", "/Run", "/TN", task_name])
        if proc.returncode != 0:
            logger.error("schtasks /Run 失败: rc=%s out=%s",
                         proc.returncode, _decode_out(proc.stderr)
                         or _decode_out(proc.stdout))
            _schtasks_delete(task_name)
            return False, manual_cmd
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("schtasks 调用异常: %s task=%s", exc, task_name)
        return False, manual_cmd
    _schedule_task_delete(task_name)
    logger.info("有头登录子进程已投放（schtasks 降级）: task=%s command=%s",
                task_name, command)
    return True, manual_cmd


# ================================================================ 启动清理


def cleanup_stale() -> None:
    """服务启动序列收敛残留（任务 #4 契约；调用方带 try/except 不阻塞启动）。

    - ``schtasks /Query /FO CSV`` 中凡 ``SAU\\HeadedLogin-*`` 一律 ``/Delete /F``
      （服务重启后内存会话已失，对应子进程即便仍在跑也回报无门，注销无害）；
    - 清理任务 XML 工作目录内全部 ``*.xml``（同属上一代进程的一次性产物）。
    """
    names: list[str] = []
    try:
        proc = _run_hidden(["schtasks", "/Query", "/FO", "CSV", "/NH"],
                           timeout=_SUBPROCESS_TIMEOUT)
        if proc.returncode == 0:
            for m in re.finditer(
                    re.escape(TASK_NAME_PREFIX) + r"[A-Za-z0-9_-]+",
                    _decode_out(proc.stdout)):
                names.append(m.group(0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("残留计划任务查询失败（跳过）: %s", exc)
    for name in dict.fromkeys(names):  # 去重保序
        _schtasks_delete(name)
    if names:
        logger.info("有头登录残留计划任务已清理: %d 个", len(set(names)))
    try:
        work_dir = paths.DATA_ROOT / _HEADED_LOGIN_DIR_NAME
        if work_dir.is_dir():
            for f in work_dir.glob("*.xml"):
                try:
                    f.unlink()
                except OSError:
                    pass
    except OSError:
        pass
