# -*- coding: utf-8 -*-
"""Agent 核心（设计文档 §3.3）。

本步（任务 #12 第一步）仅提供空跑骨架 ``core.py``；
WS 客户端 / 任务调度 / 账号管理在后续步骤（实施计划 S2）实现：

- ws_client.py   WS 客户端（连 opcgeo、重连退避 2s→300s、凭证类关闭码挂起）
- dispatcher.py  任务调度（派发上传任务、素材下载、结果回执）
- accounts.py    账号管理（cookies 存取、状态快照、account_sync 上报）

可靠性语义（§3.8 八项清单）届时逐条原样保留。
"""
