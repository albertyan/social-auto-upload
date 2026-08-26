# -*- coding: utf-8 -*-
"""平台 CLI 透传桥（终审修复⑥；设计文档 §3.2 规格表 + 决策表行 8）。

设计口径（§3.2 说明行）：上游 ``sau_cli.py`` 为按平台分组的 argparse 子命令
（``douyin`` / ``kuaishou`` / ``xiaohongshu`` / ``bilibili`` / ``tencent`` /
``youtube``，每平台 ``login`` / ``cookie-auth`` / ``check`` / ``upload-video`` /
``upload-note`` 等动作），包装层入口**保持同构透传**——即
``sau.exe <platform> <action> ...`` 与 ``python sau_cli.py <platform> ...`` 同形。

决策表行 8 定案：**转发上游 ``sau_cli.py`` 既有命令（import 或 subprocess），
不重写**，上游升级自动跟随。本模块即该转发实现：

1. 优先 ``import sau_cli`` 调其 ``main(argv)``（同进程、与冻结产物共享同一
   解释器与依赖面，冻结形态下 ``sau_cli`` 经 ``--include-module`` 进产物）；
2. import 不可行（如冻结产物漏含该模块）时回退同一解释器 ``subprocess`` 跑
   仓库内 ``sau_cli.py`` 源文件（源码开发形态兜底）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

#: 上游 CLI 支持的平台（与 ``sau_cli.py`` 的 argparse 分组一致，§3.2）
UPSTREAM_PLATFORMS = (
    "douyin", "kuaishou", "xiaohongshu", "bilibili", "tencent", "youtube",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def forward(argv: list[str]) -> int:
    """把完整上游参数（含平台首段）转发给 ``sau_cli.main``；返回退出码。

    参数示例：``["douyin", "check", "--account", "acc1"]``。
    """
    # 路径一（优先）：import 调用（同进程；冻结产物内 sau_cli 已编译进包）
    try:
        import sau_cli  # noqa: PLC0415（上游，惰性导入避免普通子命令背重依赖）
    except ImportError as exc:
        print(f"[sau] 上游 sau_cli import 失败（{exc}），尝试源码回退…",
              file=sys.stderr)
    else:
        return int(sau_cli.main(list(argv)) or 0)
    # 路径二（回退）：同一解释器 subprocess 跑上游源文件（源码开发形态）
    script = _repo_root() / "sau_cli.py"
    if script.is_file():
        return subprocess.call([sys.executable, str(script), *argv])
    print("[错误] 上游 sau_cli 不可用：import 失败且源文件缺失，"
          "无法透传平台 CLI", file=sys.stderr)
    return 1
