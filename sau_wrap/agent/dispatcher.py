# -*- coding: utf-8 -*-
"""任务调度 —— 【占位】实施计划 S2 实现。

职责（设计文档 §3.6 / §3.8）：
- 接收 opcgeo 派发的上传任务，落本地 ``local_tasks`` 快照；
- 素材下载（按任务隔离到 ``downloads/{task_id}``，错误分类可重试/不可重试）；
- import 上游 ``uploader`` 执行平台上传；
- 执行结果入 ``result_queue``，WS 在线时消费上报、断线留存自动补发。
"""


class TaskDispatcher:  # pragma: no cover
    """【占位】任务调度器（S2 实现）。"""

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError("任务调度将在实施计划 S2 实现")
