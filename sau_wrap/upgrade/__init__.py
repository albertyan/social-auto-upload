# -*- coding: utf-8 -*-
"""升级编排 —— 【占位】实施计划 S7 实现（设计文档 §7.4）。

- orchestrator.py  服务端编排：校验 → 下载 → runner 副本 → 静默安装 → 校验 → 回滚
- runner.py        runner 副本：停服 → 备份 → 安装 → 启服 → 轮询校验

状态机八态持久化于 ``%ProgramData%\\SAU\\updates\\etc\\upgrade_state.json``（§3.8 第 4 条）。
"""
