# -*- coding: utf-8 -*-
"""服务宿主与服务管理（设计文档 §3.3 / 第 4 章）。

本步实现：
- host.py   ``agent`` 命令的 pywin32 服务化封装（``--startup auto`` 惯例）
- ops.py    ``service install|remove|start|stop|status|upgrade``（本步实现前五项）

【后续步骤实现】（实施计划）：
- server.py    5409 本地 API（aiohttp，S3）
- routes.py    路由与处理器（含登录会话族、升级端点族，S3）
- ui_host.py   GET /ui/t/<token>、GET /ui/*（S3/S6）
- token.py     一次性令牌签发与校验、会话表（S3）
"""
