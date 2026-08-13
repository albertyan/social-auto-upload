# SAU Agent 打包流程

## 概述

打包分两个阶段：
1. **Nuitka 编译**：将 Python 源码编译为 Windows 可执行文件（产出到 `dist/`）
2. **Inno Setup 打包**：将 Nuitka 产物封装为 Windows 安装程序（产出到 `packaging/installer/output/`）

关系：Nuitka 产出"能跑的程序"，Inno Setup 把程序变成"能装的安装包"。

---

## 前置条件

| 依赖 | 版本要求 | 说明 |
|---|---|---|
| Python | 3.10 ~ 3.12 | 项目 `.venv` 中 |
| Nuitka | 4.x | `uv pip install nuitka ordered-set zstandard` |
| C 编译器 | MinGW64 或 MSVC | Nuitka 首次运行自动下载 MinGW64 |
| Inno Setup | 6.3+ | `D:\Program Files (x86)\Inno Setup 6\` |
| 包装层依赖 | — | `uv pip install pywin32 pystray Pillow websockets` |

---

## Step 1: 激活环境

```powershell
cd d:\dev\workspace\social-auto-upload
.venv\Scripts\activate
```

---

## Step 2: Nuitka 构建

### 预演（不实际构建）

```powershell
python packaging/nuitka_build.py --dry-run
```

### 构建全部目标

```powershell
python packaging/nuitka_build.py --target all
```

### 指定版本号（可选）

```powershell
python packaging/nuitka_build.py --target all --version 0.2.0
```

### 单独构建某个目标

代码改动后只需重建对应目标：

```powershell
python packaging/nuitka_build.py --target sau          # → dist/sau.exe
python packaging/nuitka_build.py --target sau-ops      # → dist/sau-ops.exe
python packaging/nuitka_build.py --target sau-service  # → dist/sau-service.dist/
python packaging/nuitka_build.py --target sau-tray     # → dist/sau-tray.dist/
```

### 产物结构

```
dist/
├── sau.exe                    # CLI（onefile，61MB）
├── sau-ops.exe                # 运维 CLI（onefile，61MB）
├── conf.example.py            # 配置模板（需确认存在）
├── sau-service.dist/          # Windows 服务（目录模式）
│   └── sau-service.exe
└── sau-tray.dist/             # 系统托盘（目录模式）
    └── sau-tray.exe
```

### 4 个目标说明

| 目标 | 入口脚本 | 模式 | 用途 |
|---|---|---|---|
| sau | `sau_cli.py` | onefile | 上游 CLI（浏览器管理/上传） |
| sau-ops | `sau_ops.py` | onefile | 运维 CLI（doctor/bind/status） |
| sau-service | `sau_service/service_host.py` | 目录模式 | Windows 服务宿主 |
| sau-tray | `sau_tray/tray_app.py` | 目录模式 | 系统托盘（账号登录） |

---

## Step 3: 冒烟验证（可选）

```powershell
dist\sau.exe --help
dist\sau-ops.exe doctor
dist\sau-ops.exe machine-code
```

---

## Step 4: Inno Setup 制作安装包

```powershell
& "D:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DVersion=0.1.0 "packaging\installer\sau.iss"
```

**产物**：`packaging\installer\output\sau-{version}.exe`（如 `sau-0.1.0.exe`，约 214 MB；OutputBaseFilename 随 `/DVersion` 参数变化）

> ⚠️ 不要传 `/DSourceDir` 参数，默认值（`..\..\dist` 相对 iss 文件）是正确的。

### 安装包自动执行的动作

1. 复制文件到 Program Files
2. `sau-service.exe install` → 注册 Windows 服务（SAUAgentService）
3. `sau-service.exe start` → 启动服务
4. `sau-ops.exe browser install` → 下载 Patchright Chromium 内核（需联网）
5. 写入注册表实现 `sau-tray.exe` 登录自启
6. 创建 `C:\ProgramData\SAU\{cookies,db,logs,etc,browsers,downloads}` 数据目录

---

## 快速重建流程（代码改动后）

```powershell
cd d:\dev\workspace\social-auto-upload
.venv\Scripts\activate

# 只重建改动的目标（例如改了 sau_ops.py → 重建 sau-ops）
python packaging/nuitka_build.py --target sau-ops

# 重新制作安装包
& "D:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DVersion=0.1.0 "packaging\installer\sau.iss"
```

---

## 已知坑点

| 问题 | 解决方案 |
|---|---|
| Nuitka 下载 MinGW64 卡住 | 运行 `python packaging/dl_mingw.py` 手动下载（支持断点续传） |
| `--cache-mode=isolated` 报错 | Nuitka 4.x 已移除该参数（已从构建脚本中删除） |
| GBK 控制台输出崩溃 | 入口文件需 `sys.stdout.reconfigure(encoding="utf-8")`（已修复） |
| ISCC 报 `Unknown constant "cmd.exe"` | 正确写法是 `{cmd}`（已修复） |
| `icon.ico` 缺失导致 ISCC 失败 | `packaging/installer/icon.ico` 必须存在 |
| `conf.example.py` 不在 dist/ | 手动 `Copy-Item conf.example.py dist\` |
| sau-service/sau-tray 不能改 onefile | 服务场景必须保持目录模式（DLL/pyd 运行必需） |

---

## 卸载行为

安装包卸载时自动：
1. 停止服务 → `sau-service.exe remove`
2. 删除自启注册表项
3. 询问是否保留 `C:\ProgramData\SAU\` 数据目录（含 Cookie）

---

## 升级安装

同 AppId 覆盖安装时，`PrepareToInstall` 会自动 stop + remove 旧服务再覆盖文件。
