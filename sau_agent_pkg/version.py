"""
sau_agent_pkg.version
~~~~~~~~~~~~~~~~~~~~~
SAU Agent 版本号单一事实源（全项目唯一硬编码点）。

其他模块一律通过 `from sau_agent_pkg.version import APP_VERSION` 引用；
packaging/nuitka_build.py 通过正则读取本文件（SAU_VERSION 环境变量优先级更高）。
"""

APP_VERSION = "1.0.0"
