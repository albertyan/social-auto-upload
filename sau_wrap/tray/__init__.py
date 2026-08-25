# -*- coding: utf-8 -*-
"""瘦托盘（实施计划 S5 已实现；设计文档第 5 章）。

形态（v1.2 定案）：
- pystray 原生菜单三项：打开控制台 / 打开日志目录 / 退出（**无启停**，
  服务只靠延迟自启 + 故障自动重启恢复）；
- 每 5 秒轮询 ``127.0.0.1:{port}/status``（托盘唯一轻量轮询，
  ``X-SAU-Local-Token`` 读 ``local_token.bin``，§3.7）；
- 命名互斥量 ``SAUTrayMutex`` 防多开（§5.3；会话本地命名，标准用户无
  SeCreateGlobalPrivilege 不能建 ``Global\\`` 前缀对象；托盘每会话一个）；
- 图标状态：在线绿 / 离线灰（Pillow 代码生成，无图片资源）；
- 日志 ``tray.log``（5MB × 3，§14.1）。

开机自启注册表 ``HKCU\\...\\Run\\SAUTray`` 由安装包写入（§5.4，属打包步骤，
本目录不涉及）。入口：``sau tray`` → :func:`sau_wrap.tray.app.run`。
"""

from sau_wrap.tray.app import run

__all__ = ["run"]
