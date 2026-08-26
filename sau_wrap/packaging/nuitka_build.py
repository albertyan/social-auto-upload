# -*- coding: utf-8 -*-
"""Nuitka 单目标构建脚本（设计文档 §7.1/§7.5/§8.3/§8.6，实施计划 S8）。

单一目标：``sau_wrap/entry.py`` → ``sau.exe``（--standalone，吸收全部子命令）。

控制台窗口策略（§7.5 实施验证点 2，本脚本定案）：
    ``--windows-console-mode=disable``——产物为 GUI 子系统，托盘/服务无双击黑窗；
    用户在终端敲 ``sau.exe ...`` 时 stdout 继承调用方终端仍可见（Windows 进程
    句柄继承语义）；致命异常时由 ``entry.py`` 的 ``AllocConsole`` 兜底弹窗排障。

构建纪律（§7.5 基线移植）：
    --jobs=4（限并行 C 编译）、CCACHE_DISABLE=1 + --disable-cache=all（禁缓存
    读写，产物可复现）、--assume-yes-for-downloads（MinGW 按需由 Nuitka 自下）、
    patchright driver 随 --include-package-data 单份进产物、--remove-output 清理
    ``*.build`` 残留。

体积优化（§8.6）：--nofollow-import-to 排除清单（见下，逐条注释依据）；
``--python-flag=no_asserts`` 剥断言；不启用 UPX（§8.6 定案：杀软误报风险）。

用法（仓库根目录）::

    python -m sau_wrap.packaging.nuitka_build [--with-console] [--force-console]

产物：``sau_wrap/packaging/out/sau.dist/``（sau.exe + ui/ + 运行时依赖）。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
REPO_ROOT: Path = _HERE.parent.parent          # social-auto-upload/
OUT_DIR: Path = _HERE / "out"                  # packaging/out
DIST_OUT: Path = OUT_DIR / "sau.dist"

# ---------------------------------------------------------------- 版本（§8.4）


def resolve_version() -> str:
    """SAU_VERSION 环境变量 > sau_wrap/version.py；不回退读上游（§8.4 定案）。"""
    env_v = os.environ.get("SAU_VERSION", "").strip()
    if env_v:
        return env_v
    src = (_HERE.parent / "version.py").read_text(encoding="utf-8")
    m = re.search(r'_BASE_VERSION\s*=\s*"([^"]+)"', src)
    if not m:
        raise SystemExit("[nuitka_build] 无法解析 sau_wrap/version.py 版本")
    return m.group(1)


def _file_version(version: str) -> str:
    """exe 资源版本（x.y.z.w）：核心三段 + 0；预发布后缀剥离。"""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    core = tuple(int(x) for x in m.groups()) if m else (2, 0, 0)
    return f"{core[0]}.{core[1]}.{core[2]}.0"


# ---------------------------------------------------------------- 包含/排除

#: 运行时必需的显式包含包（上游平台上传器为懒加载/动态 import，Nuitka 静态
#: 分析不可达，必须 --include-package 声明，§7.5）
INCLUDE_PACKAGES = (
    # 包装层全量
    "sau_wrap",
    # 上游运行时（只读引用，不修改，§3.4 依赖方向）
    "uploader",          # 各平台上传器（dispatcher 经 upstream_adapter 懒加载）
    "utils",             # 上游工具（日志/浏览器初始化/网络重试）
    "myUtils",           # 上游账号/凭证工具
    # 三方依赖（部分为动态 import 或数据文件依赖）
    "click", "aiohttp", "websockets",
    "patchright",        # 浏览器自动化主线（含 driver 数据）
    "playwright",        # 上游 baijiahao 链路仍引 playwright.async_api
    "pystray", "PIL",    # 瘦托盘（S5）
    "qrcode", "loguru", "requests", "certifi",
    # 注：biliup 不列入——上游 runtime.py 经 subprocess 调独立二进制（biliup.exe
    # 首次使用时按需下载至 ~/.social-auto-upload/tools/biliup，与包分发解耦），
    # 包进产物会带入 stream_gears/numpy 等 ~58MB 无效依赖；
    # aiofiles / yaml（PyYAML）亦未列入——当前 venv 未安装，且包装层运行时未引用。
    # 清单须与 site-packages 一致，否则 Nuitka FATAL（§7.5 纪律）。
)

#: 显式包含模块（pywin32 为模块级命名，非包）
INCLUDE_MODULES = (
    "win32api", "win32service", "win32serviceutil", "win32event",
    "win32con", "win32security", "win32process", "pywintypes",
    "servicemanager", "conf",  # conf：uploader 运行期 `from conf import …`
)

#: 体积优化排除清单（§8.6 手段②，逐条依据）
EXCLUDES = (
    # 测试框架（§8.6：测试框架不进产物）
    "pytest", "unittest", "doctest", "setuptools", "pip", "wheel",
    # GUI 工具箱（产物无 tkinter 依赖）
    "tkinter",
    # sau_backend.py 服务端遗留链路（包装层不承载上游本地后端，§2.3）
    "flask", "flask_cors", "werkzeug", "jinja2_ext",
    "sqlalchemy", "alembic", "mako",
    # 直播/流媒体遗留（上游 sau_cli 直播链路，包装层任务面不含）
    "streamlink", "streamlink_cli", "eventlet", "m3u8", "ykdl",
    # 并发模型遗留（playwright 用 greenlet，trio 仅测试链路）
    "trio",
    # 上游旧 xhs_uploader 遗留链路（§8.6 明确排除；现行小红书走 patchright）
    "xhs",
    # 旧前端/托盘目录（冲突 1 定案不复用；sau_tray 已由 sau_wrap/tray 替代）
    "sau_frontend", "sau_backend", "sau_tray",
    # 体积裁剪（首构建 400MB 实测后追加，§8.6）：
    # cv2（98.6MB）：仅上游 utils/login_qrcode.py 登录二维码链路引用，
    # 登录会话族不在包装层承载范围（/login 501 占位，§6.5 留后续步骤）
    "cv2",
    # numpy（~26MB）：仅作为 cv2 伴生依赖存在，上游上传链路无直接引用
    "numpy",
    # stream_gears（32.5MB）：biliup 内部推流依赖；biliup 经独立二进制调用，
    # Python 包不进产物（见 INCLUDE_PACKAGES 注释）
    "stream_gears",
)


# ---------------------------------------------------------------- 构建


def build_console_if_needed(force: bool) -> None:
    dist = _HERE.parent / "console" / "dist"
    if dist.is_dir() and (dist / "index.html").is_file() and not force:
        print(f"[nuitka_build] 控制台 dist 已存在：{dist}")
        return
    from sau_wrap.packaging.build_console import build  # noqa: PLC0415

    build(force=force)


def build_nuitka_args(version: str) -> list[str]:
    args = [
        sys.executable, "-m", "nuitka",
        str(REPO_ROOT / "sau_wrap" / "entry.py"),
        "--standalone",
        f"--output-dir={OUT_DIR}",
        "--output-filename=sau.exe",
        # 控制台窗口策略（见模块 docstring）：无黑窗，终端继承输出可见
        "--windows-console-mode=disable",
        # 构建纪律（§7.5）
        "--jobs=4",
        "--disable-cache=all",
        "--assume-yes-for-downloads",   # MinGW 缺失时由 Nuitka 自动下载
        "--remove-output",              # 清理 *.build 残留
        "--python-flag=no_asserts",     # 体积优化③：剥断言
        # 禁 playwright 插件：会把浏览器二进制拉进产物（首构建实测 +100MB），
        # 内核走 browser install 独立安装（§8.7 定案）；patchright driver 不受影响
        "--disable-plugin=playwright",
        # 元数据
        "--product-name=SAU",
        f"--product-version={_file_version(version)}",
        f"--file-version={_file_version(version)}",
        "--file-description=SAU Client (single entry wrapper)",
    ]
    for pkg in INCLUDE_PACKAGES:
        args.append(f"--include-package={pkg}")
    for mod in INCLUDE_MODULES:
        if mod == "conf" and not (REPO_ROOT / "conf.py").is_file():
            print("[nuitka_build] 仓库无 conf.py，跳过 --include-module=conf")
            continue
        args.append(f"--include-module={mod}")
    for ex in EXCLUDES:
        args.append(f"--nofollow-import-to={ex}")
    # patchright driver 单份进产物（§7.5）：含 node.exe / cli.js / browsers.json
    args.append("--include-package-data=patchright")
    args.append("--include-package-data=certifi")
    # 数据文件（§7.1）
    console_dist = _HERE.parent / "console" / "dist"
    if not (console_dist / "index.html").is_file():
        raise SystemExit(f"[nuitka_build] 控制台 dist 缺失：{console_dist}")
    args.append(f"--include-data-dir={console_dist}=ui")
    args.append(f"--include-data-files={REPO_ROOT / 'conf.example.py'}"
                f"={'conf.example.py'}")
    # pywin32 服务宿主伴随产物（真机缺陷修复，2026-08-26）：
    # 冻结形态下 InstallService 传入的 exeName 存在时 SCM 直接以 sau.exe 为宿主，
    # 但 pythonservice.exe 随包兼作兜底（pywin32 回退查找路径、旧版行为差异），
    # 与 sau.exe 同级；DLL（pywintypes312/pythoncom312）Nuitka 已自动携带。
    import win32serviceutil  # noqa: PLC0415

    pythonservice = (Path(win32serviceutil.__file__).resolve().parent.parent
                     / "pythonservice.exe")
    if not pythonservice.is_file():
        raise SystemExit(f"[nuitka_build] 未找到 pywin32 伴随产物：{pythonservice}")
    args.append(f"--include-data-files={pythonservice}=pythonservice.exe")
    if (REPO_ROOT / "skills").is_dir():
        args.append(f"--include-data-dir={REPO_ROOT / 'skills'}=skills")
    if (REPO_ROOT / "static").is_dir():
        args.append(f"--include-data-dir={REPO_ROOT / 'static'}=static")
    return args


def dir_size_mb(path: Path) -> float:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 * 1024)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nuitka 单目标构建（§7.1）")
    parser.add_argument("--force-console", action="store_true",
                        help="强制重建控制台 dist")
    args = parser.parse_args(argv)

    version = resolve_version()
    print(f"[nuitka_build] 版本：{version}（SAU_VERSION > version.py，§8.4）")
    build_console_if_needed(force=args.force_console)

    if DIST_OUT.is_dir():
        shutil.rmtree(DIST_OUT, ignore_errors=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["CCACHE_DISABLE"] = "1"          # §7.5：禁 ccache
    env["PYTHONIOENCODING"] = "utf-8"
    nuitka_args = build_nuitka_args(version)
    print("[nuitka_build] 执行 Nuitka（耗时较长，耐心等待）：")
    print("  " + " ".join(f'"{a}"' if " " in a else a for a in nuitka_args))
    proc = subprocess.run(nuitka_args, cwd=str(REPO_ROOT), env=env)
    if proc.returncode != 0:
        print(f"[nuitka_build] Nuitka 构建失败（exit={proc.returncode}）",
              file=sys.stderr)
        return proc.returncode

    # Nuitka 4.1.3 无 --output-dir-name：产物目录默认随入口文件名（entry.dist），
    # 统一改名为 sau.dist（sau.iss 的 BuildDir 定案路径）
    legacy = OUT_DIR / "entry.dist"
    if not DIST_OUT.is_dir() and legacy.is_dir():
        legacy.rename(DIST_OUT)
        print(f"[nuitka_build] 产物目录已改名：entry.dist → sau.dist")

    exe = DIST_OUT / "sau.exe"
    ui_index = DIST_OUT / "ui" / "index.html"
    if not exe.is_file():
        print(f"[nuitka_build] 产物缺失：{exe}", file=sys.stderr)
        return 1
    size_mb = dir_size_mb(DIST_OUT)
    print("\n==== 构建结果 ====")
    print(f"产物目录：{DIST_OUT}")
    print(f"sau.exe：{exe.stat().st_size / (1024*1024):.1f} MB")
    print(f"ui/index.html：{'存在' if ui_index.is_file() else '缺失！'}")
    print(f"standalone 总大小：{size_mb:.1f} MB"
          f"（≤100MB 验收口径为 Inno 安装包，见 §8.6）")
    print(f"下一步：ISCC 编译 sau_wrap/packaging/installer/sau.iss"
          f"（/DMyAppVersion={version}）→ sau-{version}.exe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
