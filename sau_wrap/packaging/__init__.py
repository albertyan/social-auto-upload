# -*- coding: utf-8 -*-
"""打包与分发（实施计划 S8，重建方案 §7/§8/§13/§17）。

- nuitka_build.py   Nuitka 单目标编译 sau_wrap/entry.py → sau.exe（§7.1/§7.5/§8.6）
- build_console.py  控制台前端构建（sau_wrap/console → dist/，§6.7）
- hash_release.py   安装包 64 位小写 SHA-256 发布单（§8.5 哈希发布闭环）
- installer/sau.iss Inno Setup 安装脚本（首装六步 §7.3 / 卸载六步 §13 / §17）
- post_install.bat  安装后编排（服务注册/启动/自启，退出码 0/11/12）
- BUILD_ENV.md      构建环境版本矩阵（§8.5）

构建顺序：前端构建先行（nuitka_build 自动触发）→ Nuitka 编译
→ ISCC 编译 sau.iss → hash_release 发布单（§8.3）。
"""
