# -*- coding: utf-8 -*-
"""升级编排（实施计划 S7；设计文档 §7.4 / §15.2，现状文档 §7）。

- ``updater.py``      通知校验 + 后台下载 + 八态状态机（``etc/upgrade_state.json`` 原子写）；
- ``orchestrator.py`` SYSTEM 无 UAC 六步编排（可注入执行器）+ 自动回滚 + 启动自检三分支。

状态机八态持久化于 ``%ProgramData%\\SAU\\etc\\upgrade_state.json``
（§3.8 第 4 条：现状文档 §7.3 原样保留）。
"""

from sau_wrap.upgrade.orchestrator import (  # noqa: F401
    MANUAL_RESCUE_GUIDE,
    Orchestrator,
    UpgradeExecutor,
    real_executors,
    resolve_install_dir,
)
from sau_wrap.upgrade.updater import (  # noqa: F401
    PHASES,
    STATE_FILE,
    Updater,
    read_state_file,
    validate_notice,
    version_gt,
)
