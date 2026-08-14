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
# 每个目标: (名称, 入口脚本, 额外参数列表)
TARGETS: dict[str, tuple[str, Path, list[str]]] = {
    "sau": (
        "sau",
        PROJECT_ROOT / "sau_cli.py",
        ["--standalone", "--onefile"],
    ),
    "sau-ops": (
        "sau-ops",
        PROJECT_ROOT / "sau_ops.py",
        ["--standalone", "--onefile"],
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
        # patchright 内置浏览器驱动（driver/node.exe + driver/package/cli.js 等）：
        # 运行时 compute_driver_executable() 按 patchright.__file__ 相对路径定位。
        # --include-package 只带 .py；--include-package-data / --include-data-dir
        # 均会过滤掉 exe；需两者配合：数据目录整体复制 + 显式补上 node.exe，
        # 否则 browser install 报 [WinError 2]
        f"--include-data-dir={_patchright_driver_dir()}=patchright/driver",
        f"--include-data-files={_patchright_driver_dir() / 'node.exe'}=patchright/driver/node.exe",
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
        # ── 输出目录 ──
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
    else:
        # 对于 standalone（非 onefile）目标，确保产物位于以目标命名的 .dist 目录内
        is_onefile = "--onefile" in extra
        if not is_onefile:
            entry_stem = entry.stem
            module_dist = DIST_DIR / f"{entry_stem}.dist"
            target_dist = DIST_DIR / f"{name}.dist"
            if module_dist.exists() and module_dist != target_dist:
                if target_dist.exists():
                    shutil.rmtree(str(target_dist))
                shutil.move(str(module_dist), str(target_dist))
                print(f"[OK] 重命名 {entry_stem}.dist → {name}.dist")
            exe_inside = target_dist / f"{name}.exe"
            if exe_inside.exists():
                print(f"[OK] {name}.exe 位于 {target_dist}/")
        print(f"[OK] {name} 构建完成 → {DIST_DIR}")
    return result.returncode


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

        print(f"\n[DONE] 全部 {len(targets_to_build)} 个目标构建成功。")


if __name__ == "__main__":
    main()
