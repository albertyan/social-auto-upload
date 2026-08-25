# -*- coding: utf-8 -*-
"""打包脚本 —— 【占位】实施计划 S8 实现（设计文档第 7 章）。

规划内容：
- nuitka_build.py   Nuitka 单目标编译 sau_wrap/entry.py → sau.exe（§7.1 / §7.5）
- build_console.py  控制台前端构建步骤（sau_wrap/console → dist/，§6.7）
- sau.iss           Inno Setup 安装脚本（首装流程 §7.3）

构建顺序：前端构建先行 → 合并依赖安装（上游 UTF-16 requirements 显式解码，§9.4）
→ Nuitka 编译 → Inno Setup 打包（§8.3）。
"""
