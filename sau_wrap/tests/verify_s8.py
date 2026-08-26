# -*- coding: utf-8 -*-
"""S8（打包与分发）链路验证脚本（任务 #26：post-install 矛盾现象修复）。

覆盖：
1. post_install.bat 退出码矩阵（假 sau.exe shim 注入受控退出码）：
   - 全链路成功 → 0；
   - service install 失败 → 11（阶段 3，日志含 FAIL 且不进入启动段）；
   - service start 三次全败 → 12（阶段 4）；
   - status 佐证异常 → 仍 0（证据步不阻断）；
2. ``ops.cmd_install`` 幂等：1073 已存在视为成功（覆盖重装不挂）、
   权限不足（5）→ SystemExit(1)、正常注册无异常；
3. 静态走查：
   - sau.iss：IntToStr 十进制、无 IntToHex、[Run] 托盘、Exec 失败映射 99、
     无「13~20 静默放行」旧条件；任务 #3 追加：卸载凭证必删（DeleteCredentialFile）、
     InitializeUninstall 弹框（MB_YESNO/IDNO，进度窗勾选框已移除）、卸载与升级两处
     taskkill 前先礼后兵（tray-exit）、内核下载入安装向导（任务 #10 改为后台下载 +
     进度文件轮询安装页进度条，静默维持隐藏阻塞等待 / 失败弹窗不阻断）、
     [Languages] 中文化（ChineseSimplified.isl 入库）；
   - post_install.bat：纯 ASCII、exit /b 0、ping 替 timeout、chcp 65001、
     数字时间戳、已移除 browser install（内核下载移至安装向导）；
   - entry.py：限秒等待回车（无 input() 无限阻塞）、控制台流跳过判定、
     --progress-file 选项（任务 #10）；
   - browser.py：ProgressReporter 原子写（os.replace）与 seq 心跳（任务 #10）；
4. ``_wait_enter_bounded`` 不无限阻塞（进程挂起根因的守卫）；
5. ProgressReporter 功能验证（任务 #10）：临时目录实例化 → 字段齐全、
   seq 严格递增、INI 可解析（configparser 模拟安装器读）、finish 后 done=1。

运行（仓库根目录）：
    .venv\\Scripts\\python.exe sau_wrap\\tests\\verify_s8.py

数据隔离：SAU_DATA_ROOT 指向本目录下 _tmpdata8。
退出码：0=全部通过；1=存在失败。报告写 ``_verify_report_s8.txt``（UTF-8）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _REPO_ROOT)
os.environ["SAU_DATA_ROOT"] = os.path.join(_HERE, "_tmpdata8")

RESULTS: list[str] = []

_BAT = os.path.join(_REPO_ROOT, "sau_wrap", "packaging", "post_install.bat")
_ISS = os.path.join(_REPO_ROOT, "sau_wrap", "packaging", "installer", "sau.iss")
_ISL = os.path.join(_REPO_ROOT, "sau_wrap", "packaging", "installer",
                    "Languages", "ChineseSimplified.isl")
_ENTRY = os.path.join(_REPO_ROOT, "sau_wrap", "entry.py")
_BROWSER = os.path.join(_REPO_ROOT, "sau_wrap", "browser.py")
_DOCTOR = os.path.join(_REPO_ROOT, "sau_wrap", "doctor.py")

_SHIM = """@echo off
rem fake sau.exe for verify_s8: exit codes injected via env vars
if "%~1"=="--version" ( echo sau 2.0.0a0-test & exit /b 0 )
if "%~1"=="service" (
  if "%~2"=="install" exit /b %FAKE_INSTALL_EXIT%
  if "%~2"=="start" exit /b %FAKE_START_EXIT%
  if "%~2"=="status" exit /b %FAKE_STATUS_EXIT%
)
exit /b 0
"""

_REG_SHIM = "@echo off\r\necho [fake reg] %*\r\nexit /b 0\r\n"


def _gbk_safe(s: str) -> str:
    """GBK 终端安全显示（检项名/证据禁含非 CP936 字符，铁律）。"""
    return s.encode("gbk", errors="backslashreplace").decode("gbk")


def check(name: str, cond: bool, evidence: str) -> None:
    status = "PASS" if cond else "FAIL"
    RESULTS.append(f"[{status}] {name} :: {evidence}")
    print(f"[{status}] {_gbk_safe(name)} :: {_gbk_safe(evidence)}", flush=True)


def _run_bat(tmp: str, install_exit: int, start_exit: int, status_exit: int) -> tuple[int, str]:
    """在临时目录跑 post_install.bat（假 sau + 假 reg 隔离），返回 (退出码, 日志)。"""
    data_dir = os.path.join(tmp, "data")
    bin_dir = os.path.join(tmp, "bin")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(bin_dir, exist_ok=True)
    bat_copy = os.path.join(tmp, "post_install.bat")
    shutil.copyfile(_BAT, bat_copy)
    shim = os.path.join(tmp, "fake_sau.cmd")
    with open(shim, "w", encoding="ascii") as f:
        f.write(_SHIM)
    with open(os.path.join(bin_dir, "reg.cmd"), "w", encoding="ascii") as f:
        f.write(_REG_SHIM)
    log = os.path.join(tmp, "post_install.log")
    env = dict(os.environ)
    env["SAU_EXE"] = shim
    env["SAU_DATA_ROOT"] = data_dir
    env["FAKE_INSTALL_EXIT"] = str(install_exit)
    env["FAKE_START_EXIT"] = str(start_exit)
    env["FAKE_STATUS_EXIT"] = str(status_exit)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    with open(log, "w", encoding="utf-8") as lf:
        # 不加额外引号：subprocess 已按参数整体传递，双重引号会致
        # "'\"path\"' 不是内部或外部命令"（与 Inno Exec 的引号模型不同）
        proc = subprocess.run(["cmd", "/c", bat_copy],
                              stdout=lf, stderr=subprocess.STDOUT, env=env,
                              cwd=tmp, timeout=120)
    with open(log, encoding="utf-8", errors="replace") as lf:
        return proc.returncode, lf.read()


# ================================================================ 场景 1：bat 退出码矩阵


def scenario_bat_matrix() -> None:
    print("\n==== 场景1：post_install.bat 退出码矩阵（假 sau.exe 注入）====", flush=True)
    root = os.path.join(_HERE, "_tmps8")
    shutil.rmtree(root, ignore_errors=True)

    # A：全链路成功 → 0（失败时落盘全量日志供排查）
    rc, log = _run_bat(os.path.join(root, "a"), 0, 0, 0)
    if rc != 0:
        print(_gbk_safe("---- A 全量日志 ----\n" + log), flush=True)
    check("矩阵A 全成功：退出码 0", rc == 0, f"rc={rc}")
    check("矩阵A 日志收尾 done 且含 install/start 退出码留痕",
          "[post-install] done" in log
          and "service install exit=0" in log
          and "service start exit=0" in log,
          f"tail={log[-160:]!r}")

    # B：install 失败 → 11，且不进入启动段（阶段归属正确）
    rc, log = _run_bat(os.path.join(root, "b"), 5, 0, 0)
    check("矩阵B install 失败：退出码 11（阶段 3）", rc == 11, f"rc={rc}")
    check("矩阵B 日志含 FAIL stage 3 与原始退出码留痕",
          "exit=5" in log and "[FAIL] service install failed [stage 3" in log,
          f"tail={log[-160:]!r}")
    check("矩阵B 未进入启动段（阶段归属不串）",
          "starting service" not in log, "no start attempts in log")

    # C：start 三次全败 → 12（含两次 10s ping，耗时 ~21s）
    rc, log = _run_bat(os.path.join(root, "c"), 0, 7, 0)
    check("矩阵C start 三连败：退出码 12（阶段 4）", rc == 12, f"rc={rc}")
    check("矩阵C 日志含三次尝试与 FAIL stage 4",
          "attempt 3/3" in log and "service start exit=7" in log
          and "[FAIL] service start failed [stage 4" in log,
          f"tail={log[-200:]!r}")

    # D：status 佐证异常 → 仍 0（证据步不阻断）
    rc, log = _run_bat(os.path.join(root, "d"), 0, 0, 1)
    check("矩阵D status 佐证异常：退出码仍 0（不阻断）", rc == 0, f"rc={rc}")
    check("矩阵D 日志含 status 异常 warn 且收尾 done",
          "service status reported anomaly" in log and "[post-install] done" in log,
          f"tail={log[-200:]!r}")


# ================================================================ 场景 2：ops.cmd_install 幂等


def scenario_ops_install_idempotent() -> None:
    print("\n==== 场景2：ops.cmd_install 幂等（1073 覆盖重装根因）====", flush=True)
    import win32service

    from sau_wrap.service import ops

    orig_install = ops.win32serviceutil.InstallService
    orig_policy = ops._apply_reliability_policy
    try:
        ops._apply_reliability_policy = lambda: []

        def _raise(winerror: int):
            def _fn(*a, **kw):
                raise win32service.error(winerror, "CreateService", "injected")
            return _fn

        # 1073 已存在 → 幂等成功（不得 SystemExit）
        ops.win32serviceutil.InstallService = _raise(1073)
        try:
            ops.cmd_install()
            exited = None
        except SystemExit as e:
            exited = e.code
        check("1073 已存在：幂等视为成功（覆盖重装不挂，无 SystemExit）",
              exited is None, f"SystemExit.code={exited!r}")

        # 权限不足（5）→ 必须 SystemExit(1)
        ops.win32serviceutil.InstallService = _raise(5)
        try:
            ops.cmd_install()
            exited = None
        except SystemExit as e:
            exited = e.code
        check("权限不足(5)：SystemExit(1) 明确报错", exited == 1,
              f"SystemExit.code={exited!r}")

        # 正常注册 → 无异常
        ops.win32serviceutil.InstallService = lambda *a, **kw: None
        try:
            ops.cmd_install()
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok = False
        check("正常注册路径：无异常返回", ok, "happy path")
    finally:
        ops.win32serviceutil.InstallService = orig_install
        ops._apply_reliability_policy = orig_policy


# ================================================================ 场景 3：静态走查


def scenario_static_walkthrough() -> None:
    print("\n==== 场景3：静态走查（消息格式化/矩阵闭环/防挂起）====", flush=True)
    iss = open(_ISS, encoding="utf-8-sig").read()
    bat_bytes = open(_BAT, "rb").read()
    bat = bat_bytes.decode("ascii")  # 纯 ASCII 纪律（解码失败即违规）
    entry = open(_ENTRY, encoding="utf-8").read()

    check("iss 十进制结果码：IntToStr(ExitCode) 在位", "IntToStr(ExitCode)" in iss,
          "unknown-code message uses IntToStr (decimal)")
    check("iss 无 IntToHex（十六进制误导源不得存在）", "IntToHex" not in iss,
          "no hex formatting")
    check("iss [Run] 段拉起托盘（托盘缺失修复）",
          "[Run]" in iss and 'Parameters: "tray"' in iss
          and "postinstall nowait skipifsilent" in iss,
          "Run section with tray postinstall")
    check("iss Exec 失败映射 99 报未知（不再伪装 -1）", "ExitCode := 99" in iss,
          "cmd launch failure -> unknown code")
    check("iss 无旧条件「13~20 静默放行」（任何非 0 都弹窗）",
          "ExitCode > 20" not in iss, "old gap condition removed")
    check("iss 阶段归属：11=阶段3 注册 / 12=阶段4 启动 文案在位",
          "阶段 3，退出码 11" in iss and "阶段 4，退出码 12" in iss,
          "stage attribution aligned with exit matrix")

    check("bat 纯 ASCII 纪律（GBK 乱码根治之一）", True, "decoded as ascii ok")
    check("bat 已移除 browser install 执行调用（任务 #3：内核下载移至安装向导）",
          not any("browser install" in l for l in bat.splitlines()
                  if not l.strip().lower().startswith("rem")),
          "no browser install invocation in executable lines")
    check("bat 含内核下载移出说明注释（口径留痕）",
          "moved to the setup wizard" in bat,
          "rem comment documents the move to wizard")
    check("bat 末尾显式 exit /b 0（兜底退出码闭环）", "exit /b 0" in bat,
          "explicit success exit")
    check("bat 以 ping 替 timeout（Session 0/重定向安全）",
          "ping -n 11" in bat
          and not any(l.strip().startswith("timeout ") for l in bat.splitlines()),
          "no executable timeout line (rem comments excluded)")
    check("bat 日志统一 UTF-8（chcp 65001）", "chcp 65001" in bat,
          "log encoding unified")
    check("bat 数字时间戳（Get-Date -Format s，无区域星期字符）",
          'Get-Date -Format s' in bat and "%date% %time%" not in bat,
          "locale-free ISO timestamp")
    check("bat 每步留痕：install/start 退出码写入日志",
          "service install exit=%RC%" in bat and "service start exit=%RC%" in bat,
          "per-step exit code evidence")

    # ---- 任务 #3：卸载数据处置 / 优雅退出 / 内核下载入向导 / 中文化 ----
    check("iss usPostUninstall 删凭证（credential.bin/local_token.bin，DeleteCredentialFile）",
          "usPostUninstall" in iss
          and "DeleteCredentialFile" in iss
          and "credential.bin" in iss and "local_token.bin" in iss,
          "credential files deleted via DeleteCredentialFile in usPostUninstall")
    cred_unconditional = iss.split("usPostUninstall:", 1)[1] if "usPostUninstall:" in iss else ""
    check("iss 凭证无条件删除（不受 DeleteLocalData 开关控制，先于整删）",
          "DeleteCredentialFile" in cred_unconditional
          and "DeleteCredentialFile" in cred_unconditional.split("if DeleteLocalData", 1)[0],
          "credential deletion precedes DeleteLocalData branch")
    check("iss InitializeUninstall 弹框询问：MB_YESNO 且默认 IDNO（默认保留）",
          "InitializeUninstall" in iss and "MB_YESNO" in iss and "IDNO" in iss,
          "yes/no prompt defaulting to keep data")
    check("iss InitializeUninstallProgressForm 已删除（旧勾选框一闪而过根因移除）",
          "InitializeUninstallProgressForm" not in iss,
          "old progress-form checkbox removed")
    check("iss 全局 DeleteLocalData 开关在位（替代进度窗勾选框）",
          "DeleteLocalData: Boolean" in iss
          and "if DeleteLocalData then" in iss,
          "global switch gates DelTree")
    check("iss 卸载/升级两处 taskkill 前均先礼后兵（tray-exit 出现 >= 2）",
          iss.count("tray-exit") >= 2
          and iss.count("taskkill.exe") >= 2,
          f"tray-exit count={iss.count('tray-exit')} taskkill count={iss.count('taskkill.exe')}")
    check("iss tray-exit 显式 --timeout 15（防贴边超时退回 taskkill）",
          "tray-exit --timeout 15" in iss,
          "explicit --timeout 15 for tray-exit")
    # ---- 任务 #10：安装页进度条（后台下载 + 进度文件轮询，替代可见控制台窗口） ----
    check("iss 内核下载入向导：后台 ewNoWait 拉起 + --progress-file 进度文件轮询",
          "browser install --progress-file" in iss
          and "ewNoWait" in iss
          and "download_progress.ini" in iss
          and "GetIniString" in iss,
          "background exec + progress file polling via GetIniString")
    check("iss 安装页进度条：借用 ProgressGauge + MapStageText 状态文案",
          "ProgressGauge" in iss
          and "正在下载浏览器内核" in iss
          and "MapStageText" in iss,
          "reuse ProgressGauge with stage caption")
    check("iss 下载期间禁用 Cancel 并 try/finally 恢复（防孤儿进程/跳过 [Run]）",
          "CancelButton.Enabled := False" in iss
          and "CancelButton.Enabled := CancelWasEnabled" in iss,
          "cancel disabled then restored in finally")
    check("iss 静默分支维持隐藏阻塞等待（ewWaitUntilTerminated + SW_HIDE）",
          "SauSilentMode()" in iss
          and "ewWaitUntilTerminated" in iss and "SW_HIDE" in iss,
          "silent branch keeps hidden blocking wait")
    check("iss done 终态 + seq 心跳停滞判定与轮询硬上限在位（任务 #10）",
          "BrowserStallSeqs" in iss and "BrowserMaxPolls" in iss
          and "'result', 'done'" in iss and "'progress', 'seq'" in iss,
          "done/seq stall detection + poll cap present")
    check("iss 无可见控制台窗口方案残留（SW_SHOWNORMAL 已移除，任务 #8 证伪）",
          "SW_SHOWNORMAL" not in iss,
          "visible-console scheme removed")
    check("iss 内核下载失败弹窗不阻断且含 --from-file 手动补救指引",
          "浏览器内核下载未完成" in iss and "--from-file" in iss,
          "warn-only popup with manual recovery hint")
    iss_exec_lines = [l for l in iss.splitlines() if not l.strip().startswith(";")]
    iss_exec = "\n".join(iss_exec_lines)
    check("iss [Languages] 仅简体中文（中文化，任务 #3）",
          "[Languages]" in iss and "chinesesimplified" in iss
          and 'Name: "english"' not in iss_exec,
          "single chinesesimplified language entry")
    check("Languages\\ChineseSimplified.isl 已入库（Inno 6.5.0+ 官方翻译）",
          os.path.isfile(_ISL), f"exists={os.path.isfile(_ISL)}")
    check("iss SauSilentMode 替代 WizardSilentMode（Inno 6.7.3 实测偏差，任务 #3）",
          "SauSilentMode" in iss and "WizardSilentMode()" not in iss_exec
          and "GetCmdTail" in iss,
          "custom silent-mode probe via GetCmdTail")

    bare_paren_echo = [
        l for l in bat.splitlines()
        if not l.strip().lower().startswith("rem") and "echo" in l.lower()
        and ("(" in l.split("echo", 1)[1] or ")" in l.split("echo", 1)[1])
    ]
    check("bat echo 文本无裸括号（if 块深度解析陷阱守卫）",
          not bare_paren_echo,
          f"offending={len(bare_paren_echo)}")

    check("entry 无 input() 阻塞调用（进程挂起根因已移除）",
          'input("' not in entry.split("def _fatal_with_console_fallback")[1],
          "fallback path has no input() call (docstring mentions excluded)")
    check("entry 限秒等待回车在位", "_wait_enter_bounded" in entry,
          "bounded wait replaces input")
    check("entry 控制台流判定在位（WriteConsoleW 保护）",
          "_stream_is_console" in entry, "console streams skip reconfigure")
    check("entry 崩溃留痕：last_crash.txt + MessageBox",
          "last_crash.txt" in entry and "MessageBoxW" in entry,
          "crash visible + persisted")

    browser = open(_BROWSER, encoding="utf-8").read()
    check("browser CFT 直链镜像在位（任务 #26：playwright 镜像路径新版 404）",
          "CFT_MIRROR_URL_TEMPLATE" in browser
          and "chrome-for-testing" in browser,
          "npmmirror CFT direct link")
    check("browser 双组件清单（完整内核+headless shell，登录 headless 必需）",
          "DIRECT_COMPONENTS" in browser
          and "chrome-headless-shell-win64.zip" in browser
          and "headless_shell_dir" in browser,
          "two-component manifest incl. headless shell")
    check("browser 安装判定覆盖双组件（is_installed 查 headless shell）",
          "headless_shell_executable()" in browser,
          "is_installed checks both components")
    bad_iter = [
        l for l in browser.splitlines()
        if "for line in proc.stdout" in l and not l.strip().startswith("#")
    ]
    check("browser 进程读行无预读（禁用 stdout 迭代器，防误判卡死根因）",
          not bad_iter and "readline()" in browser,
          "readline without read-ahead")
    check("browser 总时限 20 分钟（安装时自动下载上限）",
          "TOTAL_TIMEOUT_SECONDS = 20 * 60.0" in browser,
          "20-minute cap for installer auto-download")
    check("browser ProgressReporter 原子写（os.replace）与 seq 心跳在位（任务 #10）",
          "ProgressReporter" in browser and "os.replace" in browser
          and "seq" in browser,
          "atomic write + seq heartbeat in ProgressReporter")
    check("entry --progress-file 选项接入 ProgressReporter（任务 #10）",
          "--progress-file" in entry and "ProgressReporter" in entry,
          "--progress-file wired to ProgressReporter")

    doctor_src = open(_DOCTOR, encoding="utf-8").read()
    check("doctor 凭证到期时间越界防护（真机 OSError 22 崩溃根治）",
          "_fmt_expire" in doctor_src and "毫秒" in doctor_src,
          "expire parse guarded incl. ms normalization")
    from sau_wrap import doctor as doctor_mod  # noqa: PLC0415
    fmt_ok = True
    for dirty in (None, "x", 1756200000, 1756200000000, 99999999999, -1):
        try:
            doctor_mod._fmt_expire(dirty)
        except Exception:  # noqa: BLE001
            fmt_ok = False
    check("doctor _fmt_expire 脏值矩阵实测不崩（毫秒/越界/负值/非数值）",
          fmt_ok, "no exception on dirty expire values")
    check("entry 崩溃弹窗有超时（MessageBoxTimeoutW，防无人值守无限阻塞）",
          "MessageBoxTimeoutW" in entry and "MessageBoxW(" not in entry,
          "crash popup auto-dismiss 15s")


# ================================================================ 场景 4：防挂起守卫


def scenario_bounded_wait() -> None:
    print("\n==== 场景4：_wait_enter_bounded 不无限阻塞 ====", flush=True)
    from sau_wrap import entry as entry_mod

    t0 = time.time()
    try:
        entry_mod._wait_enter_bounded(1)
        elapsed = time.time() - t0
        ok = elapsed < 3.0
    except Exception as exc:  # noqa: BLE001
        elapsed = -1.0
        ok = False
    check("_wait_enter_bounded(1) 约 1 秒返回且无异常（无控制台不挂起）",
          ok, f"elapsed={elapsed:.2f}s")


# ================================================================ 场景 5：ProgressReporter 功能验证（任务 #10）


def scenario_progress_reporter() -> None:
    print("\n==== 场景5：ProgressReporter 进度文件功能验证（任务 #10）====", flush=True)
    import configparser

    from sau_wrap.browser import ProgressReporter

    tmp = tempfile.mkdtemp(prefix="sau_prog_")
    try:
        pf = os.path.join(tmp, "download_progress.ini")
        rep = ProgressReporter(pf)
        rep.update(stage="starting", percent=0)
        rep.update(stage="downloading", component=1, components=2, percent=47,
                   downloaded_bytes=120000000, total_bytes=301000000,
                   source="mirror", message="direct download")
        # 增量合并验证：仅更新 stage/percent，其余字段应保留旧值
        rep.update(stage="extracting", percent=60)
        rep.finish(0)

        cp = configparser.ConfigParser()
        cp.read(pf, encoding="ascii")
        prog = dict(cp["progress"])
        res = dict(cp["result"])
        need = ["stage", "component", "components", "percent", "downloaded_bytes",
                "total_bytes", "source", "message", "seq"]
        missing = [k for k in need if k not in prog]
        check("进度文件字段齐全且增量合并保留旧值（configparser 可解析）",
              not missing and prog.get("source") == "mirror"
              and prog.get("component") == "1",
              f"missing={missing} source={prog.get('source')!r}")
        check("finish 写 [result] done=1（安装器收尾判定）",
              res.get("done") == "1", f"done={res.get('done')!r}")
        check("finish 写 [result] exit_code=0（成败判定）",
              res.get("exit_code") == "0", f"exit_code={res.get('exit_code')!r}")
        check("进度文件值域纯 ASCII（Inno 无 BOM 按 GBK 解析约束）",
              bool(open(pf, "rb").read().decode("ascii") is not None),
              "decode as ascii ok")

        # seq 严格递增：每次写盘 +1（心跳）
        rep2 = ProgressReporter(os.path.join(tmp, "seq.ini"))
        seqs = []
        for pct in (10, 20, 30, 40):
            rep2.update(percent=pct)
            cp2 = configparser.ConfigParser()
            cp2.read(os.path.join(tmp, "seq.ini"), encoding="ascii")
            seqs.append(int(cp2["progress"]["seq"]))
        check("seq 每次写盘严格 +1（安装器停滞判崩溃基准）",
              len(set(seqs)) == len(seqs)
              and all(b == a + 1 for a, b in zip(seqs, seqs[1:])),
              f"seqs={seqs}")

        # 上报失败不影响主链路：父路径为普通文件 → mkdir 必抛 NotADirectoryError
        # （OSError 子类），必须被吞掉不上抛（进度上报绝不影响下载主链路）
        blocker = os.path.join(tmp, "blocker_file")
        open(blocker, "w", encoding="ascii").close()
        rep3 = ProgressReporter(os.path.join(blocker, "p.ini"))
        try:
            rep3.update(stage="downloading")
            swallow_ok = True
        except Exception:  # noqa: BLE001
            swallow_ok = False
        check("ProgressReporter 写盘异常不上抛（绝不影响下载主链路）",
              swallow_ok, "OSError swallowed on unwritable path")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    scenario_bat_matrix()
    scenario_ops_install_idempotent()
    scenario_static_walkthrough()
    scenario_bounded_wait()
    scenario_progress_reporter()

    failed = [r for r in RESULTS if r.startswith("[FAIL]")]
    report = os.path.join(_HERE, "_verify_report_s8.txt")
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(RESULTS) + f"\n\n总计 {len(RESULTS)} 项，失败 {len(failed)} 项\n")
    print(f"\n总计 {len(RESULTS)} 项，失败 {len(failed)} 项；报告: {report}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
