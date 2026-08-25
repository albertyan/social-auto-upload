# -*- coding: utf-8 -*-
"""``python -m sau_wrap`` 入口。

两种等价用法：
- ``python -m sau_wrap <子命令>``（仓库根目录下）
- ``python <repo>\\sau_wrap\\__main__.py <子命令>``（服务 ImagePath 源码形态使用，
  SCM 工作目录不可控，故此处显式引导 ``sys.path`` 与工作目录）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _bootstrap() -> None:
    """直接以脚本方式运行时（非 -m / 非包上下文），引导包可导入。"""
    if __package__ in (None, ""):
        repo_root = Path(__file__).resolve().parent.parent
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        # 服务进程工作目录显式设置为程序所在目录（设计文档 §4.1：
        # 避免 Session 0 下相对路径歧义；打包形态即 {app}）
        try:
            os.chdir(Path(__file__).resolve().parent.parent)
        except OSError:
            pass


_bootstrap()

from sau_wrap.entry import main  # noqa: E402

if __name__ == "__main__":
    main()
