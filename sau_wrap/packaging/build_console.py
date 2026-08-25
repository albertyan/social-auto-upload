# -*- coding: utf-8 -*-
"""控制台前端构建步骤（设计文档 §7.2/§8.3：前端构建先行）。

``sau_wrap/console``（Vue3 + Vite）→ ``sau_wrap/console/dist``。
dist 与 node_modules 不入库（``console/.gitignore``），Nuitka 构建经
``--include-data-dir=sau_wrap/console/dist=ui`` 内嵌进产物（§7.1）。

用法（仓库根目录）::

    python -m sau_wrap.packaging.build_console [--force]

退出码 0 = 成功（含产物已存在且未强制重建的情况）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
CONSOLE_DIR: Path = _HERE.parent / "console"
DIST_DIR: Path = CONSOLE_DIR / "dist"


def build(force: bool = False) -> Path:
    """构建控制台 dist；已存在且不强制时跳过。返回 dist 目录。"""
    if DIST_DIR.is_dir() and (DIST_DIR / "index.html").is_file() and not force:
        print(f"[build_console] dist 已存在，跳过构建：{DIST_DIR}")
        return DIST_DIR
    if not CONSOLE_DIR.is_dir():
        raise SystemExit(f"[build_console] 控制台目录不存在：{CONSOLE_DIR}")
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise SystemExit("[build_console] 未找到 npm：请先安装 Node.js（§8.2）")
    env = dict(os.environ)
    for step, args in (
        ("npm ci", [npm, "ci", "--no-audit", "--no-fund"]),
        ("npm run build", [npm, "run", "build"]),
    ):
        print(f"[build_console] 执行 {step}（cwd={CONSOLE_DIR}）", flush=True)
        proc = subprocess.run(args, cwd=str(CONSOLE_DIR), env=env)
        if proc.returncode != 0:
            raise SystemExit(f"[build_console] {step} 失败（exit={proc.returncode}）")
    if not (DIST_DIR / "index.html").is_file():
        raise SystemExit("[build_console] 构建完成但 dist/index.html 缺失")
    print(f"[build_console] 构建完成：{DIST_DIR}")
    return DIST_DIR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 sau_wrap/console 前端产物")
    parser.add_argument("--force", action="store_true", help="dist 已存在也强制重建")
    args = parser.parse_args(argv)
    build(force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
