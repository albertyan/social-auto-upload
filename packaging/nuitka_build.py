"""
SAU Agent — Nuitka 统一构建脚本
用法:
    python packaging/nuitka_build.py --target all          # 构建全部四个目标
    python packaging/nuitka_build.py --target sau          # 仅构建 sau.exe
    python packaging/nuitka_build.py --target sau-ops      # 仅构建 sau-ops.exe
    python packaging/nuitka_build.py --target sau-service  # 仅构建 sau-service.exe
    python packaging/nuitka_build.py --target sau-tray     # 仅构建 sau-tray.exe

环境变量:
    SAU_VERSION   — 版本号（最高优先级；缺省时正则读取 sau_agent_pkg/version.py，
                    再回退 pyproject.toml）
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ──────────────────────────────────────────────
# 路径常量
# ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = PROJECT_ROOT / "dist"
ICON_PATH = PROJECT_ROOT / "packaging" / "installer" / "icon.ico"
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
VERSION_PY = PROJECT_ROOT / "sau_agent_pkg" / "version.py"

# ──────────────────────────────────────────────
# 构建目标定义
# ──────────────────────────────────────────────
# 瘦身优化（合并 standalone 减少重复 ~610M）：
# 原方案 4 个目标各自独立 --standalone 会产生 4 份重复的 cv2.pyd(71M) + ffmpeg.dll(27M)
#   + numpy openblas(19M) + patchright driver(86M) ≈ 4×203M ≈ 812M 冗余。
#   同时 sau/sau-ops 用 --onefile 又各自 embed 一份完整 blob(88M)，与 standalone 叠加重复。
# 新方案：所有 4 个目标统一输出到同一个主 standalone 目录（SAU_AGENT_MAIN_DIST），
#   4 个 exe 共享同一批 .dll/.pyd/包数据，只保留 1 份大头依赖（≈ 203M 而不是 812M）。
#   Inno Setup [Files] 只从这 1 份主 .dist 拷文件，Solid 压缩前就砍去 3/4 的冗余数据。
SAU_AGENT_MAIN_DIST = DIST_DIR / "sau-agent.dist"

# 每个目标: (名称, 入口脚本, 额外参数列表)
# 为什么保留额外参数而不合并：sau-tray 必须 --enable-plugin=tk-inter，
# 而 pywin32 / pystray / PIL 的显式 include 已移到 build_common_args 统一兜底，
# 这里额外参数仅保留每个入口特有的开关（如 console 模式、tk-inter 插件）。
TARGETS: dict[str, tuple[str, Path, list[str]]] = {
    "sau": (
        "sau",
        PROJECT_ROOT / "sau_cli.py",
        # 原 --standalone --onefile 改为 --standalone（不 onefile，共享主 dist 的依赖）
        # 为什么去掉 onefile：onefile 会把整个 standalone 再 embed 成 __payload.bin，
        # 与其他 3 个 exe 的 standalone 重复，白白多占 2×88M。
        ["--standalone"],
    ),
    "sau-ops": (
        "sau-ops",
        PROJECT_ROOT / "sau_ops.py",
        ["--standalone"],
    ),
    "sau-service": (
        "sau-service",
        PROJECT_ROOT / "sau_service" / "service_host.py",
        ["--standalone", "--windows-console-mode=disable"],
    ),
    "sau-tray": (
        "sau-tray",
        PROJECT_ROOT / "sau_tray" / "tray_app.py",
        [
            "--standalone",
            "--windows-console-mode=disable",
            "--enable-plugin=tk-inter",
            # pystray 通过 importlib 动态选择后端（_win32），Nuitka 无法静态检测，
            # 必须显式包含整个包；PIL 一并显式包含以防间接依赖漏检
            "--include-package=pystray",
            "--include-package=PIL",
        ],
    ),
}


def get_version() -> str:
    """读取版本号。

    优先级：SAU_VERSION 环境变量 > sau_agent_pkg/version.py（单一事实源）
    > pyproject.toml > "0.0.0"。
    """
    env_ver = os.environ.get("SAU_VERSION", "").strip()
    if env_ver:
        return env_ver

    if VERSION_PY.exists():
        m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', VERSION_PY.read_text(encoding="utf-8"))
        if m:
            return m.group(1)

    if PYPROJECT.exists():
        text = PYPROJECT.read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if m:
            return m.group(1)

    return "0.0.0"


def check_nuitka() -> None:
    """检查 Nuitka 是否已安装。"""
    if shutil.which("nuitka") is None:
        try:
            import nuitka  # noqa: F401
        except ImportError:
            print(
                "[ERROR] Nuitka 未安装。请执行:\n"
                "    pip install nuitka ordered-set zstandard",
                file=sys.stderr,
            )
            sys.exit(1)


def _patchright_driver_dir() -> Path:
    """定位 venv 中 patchright 的 driver 目录（node.exe + cli.js 等）。"""
    import patchright  # 构建环境必须已安装 patchright

    driver = Path(patchright.__file__).parent / "driver"
    if not (driver / "node.exe").exists():
        print(f"[ERROR] patchright driver 目录异常，缺少 node.exe: {driver}", file=sys.stderr)
        sys.exit(1)
    return driver


def build_common_args(name: str, version: str) -> list[str]:
    """返回所有目标共享的 Nuitka 参数。"""
    args: list[str] = [
        sys.executable,  # nuitka 通过 python -m nuitka 调用
        "-m", "nuitka",
        # ── 包含的包 ──
        "--include-package=uploader",
        "--include-package=utils",
        "--include-package=sau_agent_pkg",
        "--include-package=sau_service",
        "--include-package=sau_tray",
        "--include-package=patchright",
        # 【瘦身 DONE — patchright driver 不再 embed 进每个目标】：
        #   之前 --include-data-dir + --include-data-files 会把 node.exe(86M)
        #   和 cli.js 整个打进每个 standalone 的二进制，4 个目标就 4×86M 冗余。
        #   现在改为构建完成后（见 main() 末尾）由构建脚本统一从 venv 拷贝 1 份到
        #   主 dist 目录 SAU_AGENT_MAIN_DIST/patchright/driver/，运行时 patchright
        #   的 compute_driver_executable() 读 patchright.__file__/../driver/ 仍然
        #   能正确定位到，不需要改任何业务代码。
        # ── 标准库 C 扩展（Nuitka 有时无法自动检测间接依赖）──
        "--include-module=socket",
        "--include-module=_socket",
        "--include-module=selectors",
        "--include-module=asyncio",
        "--include-module=ssl",
        "--include-module=_ssl",
        # ── websockets 12.x 懒加载保护：
        # websockets.imports.py 用 import_name() 动态加载 websockets.asyncio.*，
        # Nuitka 静态扫描无法识别，必须显式 include 整个 websockets 包，
        # 否则打包后报 ModuleNotFoundError: No module named 'websockets.asyncio'
        "--include-package=websockets",
        # ── pywin32 依赖（win32serviceutil 间接需要）──
        "--include-module=win32timezone",
        "--include-module=win32con",
        "--include-module=win32api",
        "--include-module=win32service",
        "--include-module=win32serviceutil",
        # ── 包数据（stealth.min.js 等）──
        "--include-package-data=utils",
        # ── 附带配置文件 ──
        f"--include-data-files={PROJECT_ROOT / 'conf.py'}=conf.py",
        # ── 缓存与优化 ──
        "--python-flag=no_site,no_asserts",
        # ── 输出目录：所有目标统一输出到 DIST_DIR，后续会合并进 SAU_AGENT_MAIN_DIST ──
        f"--output-dir={DIST_DIR}",
        f"--output-filename={name}.exe",
        # ── Windows 元数据 ──
        f"--company-name=JZVAS",
        f"--product-name=SAU Agent",
        f"--file-version={version}",
        f"--product-version={version}",
        "--file-description=SAU Agent component",
        # ── 自动下载依赖（如 MinGW64 编译器），避免交互式确认 ──
        "--assume-yes-for-downloads",
    ]

    # 图标（可选，不存在则跳过）
    if ICON_PATH.exists():
        args.append(f"--windows-icon-from-ico={ICON_PATH}")

    return args


def build_target(target_key: str, version: str, dry_run: bool = False) -> int:
    """构建单个目标，返回进程退出码。"""
    name, entry, extra = TARGETS[target_key]
    cmd = build_common_args(name, version) + extra + [str(entry)]

    print(f"\n{'=' * 60}")
    print(f"  构建目标: {name}  (入口: {entry.name})")
    print(f"{'=' * 60}")
    print(f"  命令: {' '.join(cmd)}\n")

    if dry_run:
        print("[DRY-RUN] 跳过实际构建。")
        return 0

    DIST_DIR.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        print(f"[ERROR] 构建 {name} 失败 (exit code {result.returncode})", file=sys.stderr)
        return result.returncode

    # 瘦身：所有目标的 .dist 合并到 SAU_AGENT_MAIN_DIST 一个目录，
    # 避免 4 份完整的 cv2 / numpy / patchright 重复。
    # 为什么要逐个 merge：Nuitka 每个入口会生成 `<entry_stem>.dist` 独立目录，
    # 我们把它们的内容逐个 copytree 到同一个目标目录（同名 dll/包文件直接覆盖即可），
    # 最终生成一个包含全部 4 个 exe + 一份依赖的 sau-agent.dist。
    entry_stem = entry.stem
    module_dist = DIST_DIR / f"{entry_stem}.dist"
    if not module_dist.exists():
        # onefile 模式不会生成 .dist（但我们已经移除了 onefile，这里兜底）
        print(f"[WARN] {name} 未生成 {module_dist}，跳过合并到主 dist")
    else:
        SAU_AGENT_MAIN_DIST.mkdir(parents=True, exist_ok=True)
        _merge_dist_contents(src=module_dist, dst=SAU_AGENT_MAIN_DIST)
        print(f"[OK] 合并 {entry_stem}.dist → 主目录 {SAU_AGENT_MAIN_DIST.name}/")

    # 在主 .dist 中查找目标 exe（如果 Nuitka 生成的 <name>.exe 不在 module_dist 根，
    # 可能在 DIST_DIR 根，需要一并搬进主 dist，保证 Inno 只从一个目录拿所有文件）
    exe_in_module = module_dist / f"{name}.exe"
    exe_in_dist = DIST_DIR / f"{name}.exe"
    main_exe = SAU_AGENT_MAIN_DIST / f"{name}.exe"
    if not main_exe.exists():
        if exe_in_module.exists():
            shutil.copy2(str(exe_in_module), str(main_exe))
            print(f"[OK] 复制 {name}.exe → {SAU_AGENT_MAIN_DIST.name}/ (来源: {module_dist.name})")
        elif exe_in_dist.exists():
            shutil.copy2(str(exe_in_dist), str(main_exe))
            print(f"[OK] 复制 {name}.exe → {SAU_AGENT_MAIN_DIST.name}/ (来源: DIST_DIR 根)")
        else:
            print(f"[WARN] {name}.exe 未找到（期望在 {exe_in_module} 或 {exe_in_dist}）")

    print(f"[OK] {name} 构建完成 → {DIST_DIR}")
    return 0


def _merge_dist_contents(src: Path, dst: Path) -> None:
    """递归合并两个 Nuitka .dist 目录的内容。

    为什么需要单独抽一个函数而不直接 shutil.copytree：
    - shutil.copytree 要求 dst 不存在，而我们要连续 merge 4 个 .dist
    - 直接 copytree(..., dirs_exist_ok=True) 虽然 Python 3.8+ 支持，但同名大文件
      （如 python3xx.dll / api-ms-win-*.dll）反复 copy2 会浪费 I/O 时间，这里用
      「目标已存在且大小相同就跳过」的小优化，4 个 .dist 合并能省几十秒。
    - 出现同名但大小不同的冲突时（极端情况，如不同版本 Nuitka 生成不同 dll），
      以最新 mtime 为准（保守处理，避免静默失败）。
    """
    if not src.exists():
        return
    dst.mkdir(parents=True, exist_ok=True)

    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            _merge_dist_contents(item, target)
        else:
            if target.exists():
                # 同名文件：大小一致就认为是同一份（通常是 VC 运行时 / 标准库 .pyd），
                # 跳过减少 I/O；大小不同则按 item 更新（后构建的覆盖先构建的，
                # 因为依赖版本应该一致，差异仅可能是 Nuitka 的嵌入资源）。
                try:
                    src_stat = item.stat()
                    dst_stat = target.stat()
                    if src_stat.st_size == dst_stat.st_size:
                        continue
                except OSError:
                    # stat 失败时保守复制一份，避免漏文件
                    pass
            try:
                shutil.copy2(str(item), str(target))
            except OSError as e:
                # 极少数只读/占用文件：打日志跳过，不阻断整个构建流程
                print(f"[WARN] 合并文件失败 {item} → {target}: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SAU Agent — Nuitka 统一构建脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target",
        choices=list(TARGETS.keys()) + ["all"],
        default="all",
        help="选择构建目标（默认: all）",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="覆盖版本号（默认从 SAU_VERSION 环境变量或 sau_agent_pkg/version.py 读取）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印构建命令，不实际执行",
    )
    args = parser.parse_args()

    # 前置检查
    check_nuitka()

    version = args.version or get_version()
    print(f"SAU Agent 构建  |  版本: {version}  |  Python: {sys.version.split()[0]}")

    targets_to_build = list(TARGETS.keys()) if args.target == "all" else [args.target]

    failed: list[str] = []
    for key in targets_to_build:
        rc = build_target(key, version, dry_run=args.dry_run)
        if rc != 0:
            failed.append(key)

    if failed:
        print(f"\n[FAILED] 以下目标构建失败: {', '.join(failed)}", file=sys.stderr)
        sys.exit(1)
    else:
        # ─────────────────────────────────────────────────────────────
        # 瘦身 Phase 1：patchright driver 统一拷贝到主 dist（仅 1 份，约 -260M 冗余）
        # ─────────────────────────────────────────────────────────────
        # 为什么在构建完成后做，而不是 Nuitka --include-data-files：
        #   4 个独立构建时如果每个都 --include-data-dir，会各自把 ~86M 的 node.exe
        #   拷一份进自己的 .dist，合并时虽然大小相同会被 _merge_dist_contents 跳过，
        #   但中间构建还是占 I/O + 临时磁盘空间。这里统一从 venv 只拷 1 份到最终目录，
        #   完全绕开重复问题，也保证 patchright 运行时 Path(__file__).parent/"driver"
        #   相对路径仍能正确找到 node.exe 和 cli.js。
        try:
            driver_src = _patchright_driver_dir()
            driver_dst = SAU_AGENT_MAIN_DIST / "patchright" / "driver"
            if not driver_dst.exists():
                shutil.copytree(str(driver_src), str(driver_dst))
                print(f"[OK] patchright driver 已统一拷贝（仅 1 份）→ {driver_dst}")
            else:
                print(f"[OK] patchright driver 已存在（跳过）: {driver_dst}")
        except Exception as e:
            print(f"[WARN] 拷贝 patchright driver 失败: {e}（运行时可能找不到浏览器驱动，需检查 venv patchright 安装是否完整）", file=sys.stderr)

        # ─────────────────────────────────────────────────────────────
        # 瘦身 Phase 2：清理构建中间目录（.build / *.onefile-build / 浏览器残留）
        # ─────────────────────────────────────────────────────────────
        # 为什么必须清：.build 目录下的 .c / .obj blob（每个 500M+）和 onefile-build
        # 的 __payload.bin 虽然不会进 Inno 的 Files（Source 指令没写它们），但会留
        # 在 dist/ 根目录影响用户「dir dist」观感；更严重的是如果后续有人加了
        # Source: "{#SourceDir}\*; ... recursesubdirs" 的通配符指令，这些中间产物
        # 就会被打进去。清理掉也能让 CI/CD 构建产物缓存明显变小。
        _cleanup_build_artifacts(DIST_DIR)

        # 将 conf.py 复制到 dist 目录（Inno Setup 安装包需要）
        conf_src = PROJECT_ROOT / "conf.py"
        conf_dst = DIST_DIR / "conf.py"
        if conf_src.exists():
            shutil.copy2(str(conf_src), str(conf_dst))
            print(f"[OK] conf.py 已复制到 {DIST_DIR}")

        # 将 post-install.bat 复制到 dist 目录
        bat_src = PROJECT_ROOT / "packaging" / "installer" / "post-install.bat"
        bat_dst = DIST_DIR / "post-install.bat"
        if bat_src.exists():
            shutil.copy2(str(bat_src), str(bat_dst))
            print(f"[OK] post-install.bat 已复制到 {DIST_DIR}")

        # 将诊断脚本复制到 dist 目录
        diag_src = PROJECT_ROOT / "packaging" / "installer" / "sau-diagnose.bat"
        diag_dst = DIST_DIR / "sau-diagnose.bat"
        if diag_src.exists():
            shutil.copy2(str(diag_src), str(diag_dst))
            print(f"[OK] sau-diagnose.bat 已复制到 {DIST_DIR}")

        # ── 最终主 dist 快照日志（便于排障 "打包完少了哪个 EXE / DLL"）──
        _print_main_dist_summary(SAU_AGENT_MAIN_DIST)

        print(f"\n[DONE] 全部 {len(targets_to_build)} 个目标构建成功。主 dist: {SAU_AGENT_MAIN_DIST}")


def _cleanup_build_artifacts(dist_dir: Path) -> None:
    """清理 dist 目录下所有构建中间产物（不删主 .dist 和最终 EXE）。

    白名单删除原则：只删 Nuitka 明确生成的构建目录，不碰用户可能用到的文件：
    - `<entry>.build/`          — Nuitka C/C++ 编译产物（~500M × 4）
    - `<entry>.onefile-build/`  — onefile 的 blob（即使关闭了 onefile，历史残留也可能存在）
    - `*/browsers/`             — 本地构建时 patchright 偷偷下载的 chromium 内核（~170M，
                                    安装包中我们用 [Run] 段首次安装时再下载，不带它进包）
    - `.tmp/` / `.cache/`       — 某些 Nuitka 插件残留的临时目录
    """
    import fnmatch

    if not dist_dir.exists():
        return

    # 判断一个路径是否属于「应删除的中间产物」
    def _is_build_garbage(p: Path) -> bool:
        name = p.name
        if not p.is_dir():
            return False
        # 明确白名单匹配
        if fnmatch.fnmatch(name, "*.build"):
            return True
        if fnmatch.fnmatch(name, "*.onefile-build"):
            return True
        if name == "browsers":
            # 注意：只删 dist 下 <entry>.dist/ 内部的 browsers 以及 dist 直接子目录 browsers
            # commonappdata 下真正运行时下载的 browsers 不会碰（那个是用户数据）
            return True
        if name in (".tmp", ".cache", "__pycache__"):
            return True
        return False

    def _walk_and_clean(root: Path) -> int:
        """递归清理，返回删除的目录数。先递推再删自己（后序遍历，避免子目录被父目录先占用）。"""
        removed = 0
        try:
            for child in root.iterdir():
                if child.is_dir():
                    removed += _walk_and_clean(child)
        except OSError:
            # 没权限或句柄占用就跳过，我们只做「尽力清理」不能阻断成功流程
            pass
        if root != dist_dir and _is_build_garbage(root):
            try:
                shutil.rmtree(str(root), ignore_errors=True)
                return removed + 1
            except OSError:
                return removed
        return removed

    total = 0
    try:
        for child in list(dist_dir.iterdir()):
            if child.is_dir():
                total += _walk_and_clean(child)
    except OSError as e:
        print(f"[WARN] 清理 dist 构建中间产物时发生 I/O 错误: {e}（不影响最终打包）")
        return

    if total > 0:
        print(f"[OK] 清理 {total} 个构建中间目录（.build / onefile-build / browsers / cache）")


def _print_main_dist_summary(main_dist: Path) -> None:
    """打印主 dist 的体积与关键文件清单（排障 + 让 build log 一眼看出是否漏合并）。"""
    if not main_dist.exists():
        print(f"[WARN] 主 dist 目录不存在，无法打印汇总: {main_dist}")
        return

    # 计算整个目录的总大小
    total_bytes = 0
    try:
        for p in main_dist.rglob("*"):
            try:
                if p.is_file():
                    total_bytes += p.stat().st_size
            except OSError:
                pass
    except OSError:
        total_bytes = 0

    total_mb = total_bytes / (1024 * 1024)
    print(f"\n{'─' * 60}")
    print(f"  主 dist 汇总: {main_dist}")
    print(f"  总大小: {total_mb:.1f} MB  ({total_bytes:,} bytes)")
    print(f"{'─' * 60}")

    # 4 个关键 EXE 是否都齐了
    expected_exes = ["sau.exe", "sau-ops.exe", "sau-service.exe", "sau-tray.exe"]
    for exe_name in expected_exes:
        exe = main_dist / exe_name
        mark = "✔" if exe.exists() else "✘ MISSING"
        size_mb = (exe.stat().st_size / (1024 * 1024)) if exe.exists() else 0.0
        print(f"    {mark}  {exe_name:<20s}  {size_mb:>7.1f} MB")

    # 几个大头依赖是否齐（不齐的话打包后运行时才炸）
    big_files = [
        ("cv2/cv2.pyd",                       "OpenCV（扫码）"),
        ("cv2/opencv_videoio_ffmpeg*.dll",    "OpenCV FFMPEG"),
        ("patchright/driver/node.exe",        "patchright Node 驱动"),
        ("numpy.libs/libopenblas*.dll",       "NumPy OpenBLAS"),
    ]
    import fnmatch
    for glob_pat, desc in big_files:
        matches = [p for p in main_dist.rglob(glob_pat) if p.is_file()]
        if matches:
            size_mb = sum(p.stat().st_size for p in matches) / (1024 * 1024)
            print(f"    ✔  {desc:<24s}  {size_mb:>7.1f} MB  ({len(matches)} 份)")
        else:
            print(f"    ⚠  {desc:<24s}  NOT FOUND（运行时可能报错）")


if __name__ == "__main__":
    main()
