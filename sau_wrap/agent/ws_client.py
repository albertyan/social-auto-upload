# -*- coding: utf-8 -*-
"""WS 客户端 —— 【占位】实施计划 S2 实现。

职责（设计文档 §3.8 / §3.5）：
- 连接 opcgeo 服务端（WSS 外连，Agent token 鉴权，协议见《SAU对接详细设计文档》§3）；
- 心跳与时钟偏差校准（滑动窗口 10 样本，偏差 > 5 分钟暂停调度）；
- 离线/弱网重连退避 2s→300s；
- 凭证类关闭码 4401 / 4403 / 4409 / 4410：挂起零重连，等待凭证恢复。
"""

raise_placeholder = "WS 客户端将在实施计划 S2 实现（本步为空跑骨架，不连接 opcgeo）"


def create_ws_client(*args, **kwargs):  # pragma: no cover
    """【占位】创建 WS 客户端（S2 实现）。"""
    raise NotImplementedError(raise_placeholder)
