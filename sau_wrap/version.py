# -*- coding: utf-8 -*-
"""版本单一事实源（设计文档 §8.4，v1.2 定案）。

规则：
- ``APP_VERSION`` 是包装层版本的唯一事实源；
- 可被环境变量 ``SAU_VERSION`` 覆盖（构建/测试场景注入）；
- 不回退读上游 ``pyproject.toml``——上游版本号与发布节奏独立，不得混用；
- 安装包命名 ``sau-{version}.exe`` 与升级校验均以此对账（§8.5）。
"""

import os

#: 包装层基线版本号（任务 #12 第一步原型）
_BASE_VERSION = "2.0.0a0"

#: 环境变量覆盖入口（设计文档 §8.4）
APP_VERSION: str = os.environ.get("SAU_VERSION", "").strip() or _BASE_VERSION


def get_version() -> str:
    """返回当前生效版本（已应用 SAU_VERSION 覆盖）。"""
    return APP_VERSION


if __name__ == "__main__":  # pragma: no cover
    print(APP_VERSION)
