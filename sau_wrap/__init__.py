# -*- coding: utf-8 -*-
"""sau_wrap —— SAU 客户端包装层（全部新增，上游源码零修改）。

目录规划见《SAU客户端重建方案-单EXE与包装层设计》§3.3：

- entry.py      sau.exe 唯一入口：子命令分发（Click）
- agent/        Agent 核心：WS 客户端、任务调度、账号管理
- service/      服务宿主 + 5409 本地 API + 服务管理（pywin32）
- tray/         瘦托盘（pystray，后续步骤实现）
- console/      控制台前端（包装层自建，后续步骤实现）
- upgrade/      升级编排（后续步骤实现）
- packaging/    打包脚本（后续步骤实现）
- version.py    APP_VERSION 版本单一事实源（§8.4）

铁律：依赖方向只能是 包装层 → 上游（单向），严禁反向（§3.4）。
"""

from sau_wrap.version import APP_VERSION

__version__ = APP_VERSION

__all__ = ["APP_VERSION", "__version__"]
