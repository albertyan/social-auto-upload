# -*- coding: utf-8 -*-
"""升级 runner 副本执行入口（终审修复⑧；设计文档 §7.4 步骤 8-10）。

服务进程收到 ``POST /upgrade/apply`` 后仅做移交（见 :mod:`orchestrator`
``spawn_runner``）：拷贝自身到 ``updates/runner/`` 并以
``DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB`` 拉起本副本，执行隐藏子命令::

    sau.exe service upgrade-run --installer <安装包> --target-version <版本>

本进程（旧版本副本，独立于将被停掉的服务进程树）执行升级全流程：
``[1/6] 停服 → [2/6] 备份 → [3/6] 杀托盘 → [4/6] 静默安装 → [5/6] 启服 →
[6/6] 轮询校验``；任一步失败自动回滚（见 :class:`Orchestrator`）。

**可注入结构保持**：``executor`` / ``updater`` / ``logger`` 均可注入——
真机执行器在本进程内构造（校验令牌每次现读文件，终审修复①：本副本与新版
服务进程各自启动时令牌已轮换，快照必 401）；测试注入假执行器走同一编排。

状态收敛：服务进程已置 ``applying``，本进程复用同一 ``upgrade_state.json``
（``Updater`` 构造即读回），编排结果（success / rolled_back / failed）直接
持久化，控制台 ``GET /upgrade`` 由新版服务进程读出终态（§15.2 启动自检兜底
本进程中途崩溃的场景）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from sau_wrap import paths


def run_upgrade(installer: str | Path,
                target_version: str,
                *,
                executor=None,
                updater=None,
                logger: logging.Logger | None = None) -> dict:
    """执行升级全流程（runner 进程主体）。返回编排结果快照。

    参数：
    - ``installer`` / ``target_version``：服务侧移交时传入（与状态机一致）；
    - ``executor``：默认 :func:`real_executors`（本进程内构造；**不带**
      ``spawn_runner`` 以免递归移交）；测试注入假执行器；
    - ``updater``：默认读同一状态文件的新实例；测试注入隔离实例；
    - ``logger``：默认 ``upgrade.log`` 轮转日志（§14.1）。
    """
    from sau_wrap.logutil import setup_logger
    from sau_wrap.upgrade.orchestrator import Orchestrator, real_executors
    from sau_wrap.upgrade.updater import Updater

    logger = logger or setup_logger("sau.upgrade.runner",
                                    paths.UPGRADE_LOG_FILE, also_console=True)
    logger.info("runner 副本启动：installer=%s target=%s",
                installer, target_version)

    port = 5409
    try:
        from sau_wrap.agent import config as agent_config
        cfg = agent_config.load_config()
        if cfg is not None and getattr(cfg, "local_api_port", None):
            port = int(cfg.local_api_port)
    except Exception:  # pragma: no cover - 配置异常不阻断编排（默认 5409）
        logger.exception("读取 local_api_port 失败，按默认 5409 校验")

    updater = updater or Updater(logger, autoschedule=False)
    executor = executor or real_executors(logger, port)
    # runner 内必须置空 spawn_runner：防递归移交（全流程就在本进程执行）。
    if getattr(executor, "spawn_runner", None) is not None:
        executor.spawn_runner = None
    orch = Orchestrator(logger, updater, executor, local_api_port=port)
    result = orch.apply()
    logger.info("runner 副本编排结束：%s", result)
    return result
