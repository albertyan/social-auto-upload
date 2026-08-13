# SAU Agent — 社交媒体自动发布 Agent

Windows 桌面端 Agent 程序，通过 WebSocket 长连接对接 opcgeo 服务端，接收社交媒体发布任务，以无头浏览器方式自动执行多平台内容上传。

**双进程架构：** Windows 系统服务（Session 0，Local System 账户）承载 Agent 核心与本地控制 API；系统托盘应用（用户会话）提供状态监控、平台登录与服务控制界面。

**支持平台：** 抖音、快手、小红书、Bilibili、视频号、YouTube、百家号

---

## 目录结构

```
social-auto-upload/
├── sau_agent_pkg/          # Agent 核心包
│   ├── config.py           #   配置管理（SAU_HOME、config.json、DPAPI 加密、local_token）
│   ├── core.py             #   WS 主循环（连接/注册/心跳/消息路由/时钟同步/断线补发）
│   ├── db_init.py          #   SQLite 数据库初始化（local_tasks、result_queue 表）
│   ├── dispatcher.py       #   任务调度器（并发控制、素材下载、上传执行、异常分类）
│   ├── local_api.py        #   本地控制 HTTP API（aiohttp，127.0.0.1:5410）
│   ├── accounts.py         #   账号扫描与 cookie 有效性检查
│   ├── machine.py          #   机器指纹采集（MachineGuid + 卷序列号 + CPU ID）
│   ├── upstream_adapter.py #   上游 API 隔离层（平台能力注册表、上传/登录/检查函数映射）
│   └── __init__.py
├── sau_service/            # Windows 系统服务
│   ├── service_host.py     #   pywin32 服务宿主（Session 0，Local System）
│   ├── runner.py           #   前台调试模式入口（不依赖 pywin32）
│   └── __init__.py
├── sau_tray/               # 系统托盘应用
│   ├── tray_app.py         #   托盘图标、状态轮询、菜单构建
│   ├── login_flows.py      #   各平台有头登录流程
│   ├── home_shim.py        #   SAU_HOME 运行时垫片（改写 conf.BASE_DIR）
│   └── __init__.py
├── packaging/              # 打包与安装
│   ├── nuitka_build.py     #   Nuitka 统一构建脚本（四个目标）
│   └── installer/
│       └── sau.iss         #   Inno Setup 安装脚本
├── uploader/               # 各平台上传器（上游）
│   ├── douyin_uploader/    #   抖音
│   ├── ks_uploader/        #   快手
│   ├── xhs_uploader/       #   小红书（xhs SDK）
│   ├── xiaohongshu_uploader/ #  小红书（备用）
│   ├── bilibili_uploader/  #   B 站（biliup）
│   ├── tencent_uploader/   #   视频号
│   ├── youtube_uploader/   #   YouTube
│   ├── baijiahao_uploader/ #   百家号
│   └── base_video.py       #   上传请求基类
├── utils/                  # 公共工具
│   ├── constant.py         #   常量定义
│   ├── files_times.py      #   文件与时间工具
│   ├── log.py              #   日志配置
│   ├── network.py          #   网络工具
│   ├── login_qrcode.py     #   登录二维码工具
│   ├── stealth.min.js      #   Playwright 反检测脚本
│   └── base_social_media.py #  社交媒体基类
├── sau_ops.py              # 统一运维 CLI（打包为 sau-ops.exe）
├── sau_cli.py              # 上游 CLI 入口（打包为 sau.exe）
├── sau_agent.py            # 旧版 Agent 入口
├── sau_backend.py          # Web 管理后台（Flask）
├── conf.py                 # 运行时配置（BASE_DIR、Chrome 路径、代理等）
├── conf.example.py         # 配置示例
├── pyproject.toml          # 项目元数据与依赖声明（uv 管理）
├── requirements.txt        # 依赖锁定列表（备用）
├── start-win.bat           # Windows 一键启动脚本（后端 + 前端）
├── examples/               # 各平台 Cookie 获取示例脚本
├── docs/                   # 文档
├── tests/                  # 测试用例
├── skills/                 # Agent Skills
├── sau_frontend/           # Web 管理前端（Vue.js）
├── static/                 # 静态资源
└── media/                  # 媒体文件
```

---

## 环境要求

| 项目 | 要求 |
|------|------|
| **Python** | 3.10 – 3.12 |
| **uv** | 最新版（项目使用 uv 管理虚拟环境与依赖） |
| **操作系统** | Windows 10 / 11（pywin32 服务、pystray 托盘、DPAPI 加密均依赖 Windows） |
| **浏览器内核** | patchright（Chromium），用于无头/有头浏览器操作 |
| **网络** | 可访问 opcgeo 服务端 WebSocket 地址 |
| **权限** | 安装/卸载服务需管理员权限；日常运行服务以 Local System 身份执行 |

---

## 安装与打包

### 开发环境搭建

本项目使用 [uv](https://docs.astral.sh/uv/) 作为 Python 环境与依赖管理工具。如未安装 uv，请先执行 `pip install uv` 或参考 [uv 安装文档](https://docs.astral.sh/uv/getting-started/installation/)。

```bash
# 1. 克隆项目
git clone <repo-url>
cd social-auto-upload

# 2. 创建虚拟环境（推荐 Python 3.11）
uv venv

# 3. 激活虚拟环境（Windows）
.venv\Scripts\activate

# 4. 以可编辑模式安装项目及全部依赖（读取 pyproject.toml）
uv pip install -e ".[agent,web]"

# 打包依赖（仅打包时需要）
uv pip install nuitka ordered-set zstandard

# 5. 复制并编辑配置文件
cp conf.example.py conf.py
# 编辑 conf.py，设置 BASE_DIR、Chrome 路径等

# 6. 安装 patchright 浏览器内核
python -m patchright install chromium
# 或设置环境变量：
# set PLAYWRIGHT_BROWSERS_PATH=C:\ProgramData\SAU\browsers
```

> **说明：** `uv pip install -e ".[agent,web]"` 会自动安装 `pyproject.toml` 中声明的全部核心依赖以及 `agent`（websockets 等）和 `web`（Flask 等）可选依赖。如需仅安装核心依赖，使用 `uv pip install -e .`。

### Nuitka 打包

统一构建脚本 `packaging/nuitka_build.py`，生成四个构建目标：

| 目标 | 入口 | 模式 | 说明 |
|------|------|------|------|
| `sau.exe` | `sau_cli.py` | `--onefile` | 上游 CLI 工具 |
| `sau-ops.exe` | `sau_ops.py` | `--onefile` | 统一运维 CLI |
| `sau-service.exe` | `sau_service/service_host.py` | `standalone` | Windows 系统服务（无控制台） |
| `sau-tray.exe` | `sau_tray/tray_app.py` | `standalone` | 系统托盘应用（无控制台） |

```bash
# 确保已安装打包依赖
uv pip install nuitka ordered-set zstandard

# 构建全部目标
python packaging/nuitka_build.py --target all

# 构建单个目标
python packaging/nuitka_build.py --target sau-ops

# 指定版本号
python packaging/nuitka_build.py --target all --version 0.1.0

# 仅打印命令，不实际构建
python packaging/nuitka_build.py --target all --dry-run
```

构建产物输出到 `dist/` 目录。`sau.exe` 和 `sau-ops.exe` 为单文件；`sau-service` 和 `sau-tray` 为 standalone 目录（含依赖 DLL/pyd）。

### Inno Setup 安装包

```bash
# 先完成 Nuitka 构建，然后执行：
iscc /DVersion=0.1.0 /DSourceDir=..\dist packaging\installer\sau.iss
```

安装包行为：
- 安装四个 EXE 及 standalone 依赖到 `{autopf}\SAU`
- 创建 `%ProgramData%\SAU` 运行时数据目录（cookies、db、logs、downloads、browsers 等）
- 注册并启动 `SAUAgentService` Windows 服务（自动延迟启动）
- 安装 patchright 浏览器内核
- 写入当前用户自启注册表项（`HKCU\...\Run\SauTray`）
- 启动托盘应用
- 创建开始菜单快捷方式（可选桌面快捷方式）
- 卸载时询问是否保留数据目录

---

## 启动与运行

### 方式一：安装包安装后运行

安装完成后系统自动完成以下配置：

- **Windows 服务** `SAUAgentService`：开机自动启动（延迟启动），以 Local System 身份运行 Agent 核心与本地 API
- **托盘应用** `sau-tray.exe`：随用户会话自动启动，在系统托盘显示状态图标

**手动服务控制：**

```bash
# 通过 CLI
sau-ops service start       # 启动服务
sau-ops service stop        # 停止服务
sau-ops service restart     # 重启服务
sau-ops service status      # 查询服务状态
sau-ops service install     # 安装服务
sau-ops service uninstall   # 卸载服务
```

**托盘图标状态：**

| 颜色 | 含义 |
|------|------|
| 🟢 绿色 | WS 已连接，服务正常 |
| 🟡 黄色 | 服务运行但 WS 未连接，或时钟偏差告警 |
| 🔴 红色 | 服务未运行 |

### 方式二：开发模式运行

各组件可独立运行，便于开发调试：

**Agent 核心 + 本地 API（前台模式）：**

```bash
# 推荐：使用 runner.py 前台运行（不依赖 pywin32）
python -m sau_service.runner
# 或
python sau_service/runner.py
```

此模式同时启动 Agent 核心（WS 连接）和本地 API 服务器（`127.0.0.1:5410`），日志输出到控制台和 `%ProgramData%\SAU\logs\sau-runner.log`。按 `Ctrl+C` 停止。

**托盘应用：**

```bash
# 通过 CLI 启动
sau-ops tray

# 或直接运行
python -m sau_tray.tray_app
# 或
python sau_tray/tray_app.py
```

> **注意：** 托盘应用依赖本地 API 服务（端口 5410）正在运行。开发时需先启动 runner.py。

**Windows 服务（需 pywin32，需管理员权限）：**

```bash
# 安装服务
python sau_service/service_host.py install

# 启动服务
python sau_service/service_host.py start

# 停止服务
python sau_service/service_host.py stop

# 卸载服务
python sau_service/service_host.py remove
```

### 方式三：命令行工具

`sau-ops` 是统一运维 CLI，提供以下子命令：

```bash
# 服务管理
sau-ops service install|uninstall|start|stop|restart|status

# 绑定 opcgeo 服务器
sau-ops bind --server wss://your-server/opcgeo/agent/ws --token <agent-token>

# 查看完整状态摘要（服务/连接/账号/配置/机器码）
sau-ops status

# 查看本机机器码
sau-ops machine-code

# 账号管理
sau-ops accounts list                          # 列出已登录账号
sau-ops accounts recheck                       # 全量检查 cookie 有效性
sau-ops accounts remove --platform douyin --account default  # 删除指定账号

# 浏览器内核管理
sau-ops browser install                        # 在线安装 patchright 内核
sau-ops browser install --from browsers.zip    # 从离线包安装

# 环境检查（检查 Python 版本、目录、依赖、配置等）
sau-ops doctor

# 前台启动托盘（调试模式）
sau-ops tray
```

---

## 配置说明

### 配置文件位置

所有运行时数据存储在 `SAU_HOME` 目录下：

```
SAU_HOME = %ProgramData%\SAU    （默认）
         或 %SAU_HOME%           （环境变量覆盖）
```

目录结构：

```
%ProgramData%\SAU\
├── config.json          # 主配置文件
├── credential.bin       # DPAPI 加密的 agent_token
├── local_token.bin      # 本地控制 API 认证令牌（服务每次启动随机生成）
├── cookies/             # 各平台 Cookie 文件（{platform}_{account}.json）
├── downloads/           # 任务素材临时下载目录
├── logs/                # 日志文件
│   ├── sau-service.log  #   服务日志
│   ├── sau-runner.log   #   前台模式日志
│   └── tasks/           #   任务详细日志
├── db/
│   └── sau.db           # SQLite 数据库（local_tasks、result_queue）
└── browsers/            # patchright 浏览器内核
```

### config.json

```json
{
  "server_url": "wss://your-server/opcgeo/agent/ws",
  "agent_id": "自动生成 UUID",
  "heartbeat_interval": 30,
  "max_concurrency": 2,
  "account_check_interval": 3600,
  "reconnect_backoff_max": 300
}
```

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `server_url` | `wss://localhost/opcgeo/agent/ws` | opcgeo 服务端 WebSocket 地址 |
| `agent_id` | 自动生成 | Agent 唯一标识（UUID） |
| `heartbeat_interval` | `30` | 心跳间隔（秒） |
| `max_concurrency` | `2` | 最大并发上传任务数 |
| `account_check_interval` | `3600` | 账号有效性检查间隔（秒） |
| `reconnect_backoff_max` | `300` | WS 重连退避上限（秒） |

### DPAPI 加密

Agent Token 使用 Windows DPAPI（`CryptProtectData`）加密存储为 `credential.bin`，采用 `CRYPTPROTECT_LOCAL_MACHINE` 级别保护。这意味着：

- 加密数据与本机绑定，无法迁移到其他机器
- 服务进程（SYSTEM）和同机器的用户进程均可解密
- 换机或重装系统后需重新绑定

### 机器指纹

机器码 = `SHA-256(MachineGuid + 系统盘卷序列号 + CPU ID)` 的前 32 位。

三者均不随应用重装变化，重装系统会改变（符合"换机/重装需重新绑定"的预期）。

### conf.py 运行时配置

```python
BASE_DIR = Path(__file__).parent.resolve()  # 被 home_shim 改写为 SAU_HOME
XHS_SERVER = "http://127.0.0.1:11901"       # 小红书 SDK 服务地址
LOCAL_CHROME_PATH = ""                       # 可选：指定 Chrome 路径
LOCAL_CHROME_HEADLESS = True                 # 默认无头模式
DEBUG_MODE = True                            # 调试模式
YT_PROXY = None                              # YouTube 上传代理（如 "http://127.0.0.1:7890"）
```

---

## 核心功能

### WebSocket 连接

Agent 核心通过 WebSocket 长连接 opcgeo 服务端，完整生命周期：

1. **连接建立** — 携带 `agentId`、`token`、`machine` 参数连接 WS
2. **注册** — 发送 `register` 消息（agent_id、machine_code、version、platforms、accounts）
3. **心跳** — 每 30s 发送 `heartbeat`（agent_id、active_tasks、accounts、clock_offset）
4. **消息路由** — 处理 `publish_task`、`account_check`、`heartbeat_ack`、`file_renewed`、`bind_rejected`、`upgrade_notice`
5. **断线重连** — 指数退避（2s → 4s → ... → 300s 上限）
6. **关闭码处理** — `4409`（机器码不匹配）、`4401/4403`（凭证无效）停止重连

**时钟同步机制：**

- 通过 `heartbeat_ack` 中的 `server_time` 计算时钟偏移（滑动平均，窗口 10 次）
- 偏差 > 5 分钟时暂停任务调度（只入队不执行），防止定时任务在错误时间触发
- 偏差恢复正常后自动恢复调度

**断线补发：**

- 任务结果（`task_result`）先写入 `result_queue` 表，WS 发送成功后删除
- 重连后自动逐条重放 `result_queue` 中未发送的结果

### 任务调度

`Dispatcher` 负责任务的完整执行流程：

```
接收 publish_task
    ↓
落 local_tasks 表（status=queued）
    ↓
等待信号量（并发控制，默认 max_concurrency=2）
    ↓
解析账号 → 检查 cookie 有效性
    ↓
下载素材（aiohttp 异步下载，video → file_url，note → media_urls[]）
    ↓
构建 UploadRequest（dataclass）
    ↓
调用 upstream_adapter 上传函数
    ↓
回报结果（task_progress → task_result）
    ↓
清理下载文件
```

**异常分类：**
- Cookie 失效 → `failed`
- 网络错误 → 请求 `file_renew`（素材重签）
- 签名 URL 过期（HTTP 403） → `FileRenewNeeded` → 请求重签
- 其他异常 → `failed`

**重启恢复：** 服务重启时 `recover_pending()` 扫描 `local_tasks` 中 `queued/running` 状态的任务，重新提交执行。

### 账号管理

- **Cookie 存储：** `SAU_HOME/cookies/{platform}_{account}.json`
- **账号扫描：** 启动时扫描 cookies 目录，解析文件名获取平台与账号信息
- **有效性检查：** 通过 `upstream_adapter.check_fns` 调用各平台检查函数
- **平台登录：** 托盘菜单触发有头（headed）浏览器登录流程，用户扫码或手动登录后保存 Cookie

### 本地控制 API

监听 `127.0.0.1:5410`，所有请求需携带 `X-SAU-Local-Token` 请求头（值来自 `local_token.bin`，服务每次启动时随机生成）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/status` | 服务状态（WS 连接、活跃任务、账号列表、时钟偏差等） |
| POST | `/login` | 返回支持的平台列表 |
| POST | `/accounts/recheck` | 触发全量 Cookie 检查 |
| POST | `/config` | 更新 `server_url` / 绑定 `token` |
| GET | `/config` | 读取非敏感配置（token 只返回是否已绑定） |
| POST | `/reload` | 配置热重载（触发 WS 重连） |

### 系统服务

基于 pywin32 的 Windows 服务实现：

- **服务名：** `SAUAgentService`
- **显示名：** `SAU Publish Agent`
- **启动类型：** 自动（延迟启动）
- **运行身份：** Local System（Session 0）
- **日志：** 文件轮转（10MB × 5）+ Windows 事件日志
- **服务恢复：** 可通过 `sc failure` 配置自动重启

服务内部同时运行 Agent 核心（WS 长连接）和本地 API 服务器，共享同一个 asyncio 事件循环。通过 `threading.Event` → `asyncio.Event` 桥接实现 SCM 停止信号的优雅处理。

### 托盘应用

基于 pystray + Pillow 的系统托盘应用：

- **状态轮询：** 每 3s 调用 `http://127.0.0.1:5410/status` 刷新状态
- **图标颜色：** 绿色（已连接）/ 黄色（未连接或时钟偏差）/ 红色（服务未运行）
- **Tooltip：** 显示连接状态文字

**菜单功能：**
- 服务控制：启动 / 停止 / 重启
- 平台登录：抖音 / 快手 / 小红书 / B 站 / 视频号 / YouTube / 百家号
- 账号状态查看与重新检查
- 绑定 opcgeo 账号向导（tkinter 输入框 → 调用本地 API）
- 打开日志目录
- 关于（显示 Agent ID、机器码、连接状态等）
- 检查更新
- 退出托盘（不停止服务）

---

## 日志与排障

### 日志文件位置

| 文件 | 路径 | 说明 |
|------|------|------|
| 服务日志 | `%ProgramData%\SAU\logs\sau-service.log` | Windows 服务运行时的日志 |
| 前台模式日志 | `%ProgramData%\SAU\logs\sau-runner.log` | 开发模式运行时的日志 |
| 任务日志 | `%ProgramData%\SAU\logs\tasks\` | 各任务的详细执行日志 |

日志格式：`%(asctime)s [%(levelname)s] %(name)s: %(message)s`

文件轮转策略：10MB × 5 备份（服务）/ 10MB × 3 备份（前台模式）

### 常见问题

**1. 托盘图标显示红色（服务未运行）**

```bash
# 检查服务状态
sau-ops service status

# 启动服务
sau-ops service start

# 若服务未安装
sau-ops service install
```

**2. WS 连接失败（黄色图标）**

- 检查 `config.json` 中 `server_url` 是否正确
- 检查 Token 是否已绑定：`sau-ops status`
- 检查网络是否可达
- 查看服务日志：`%ProgramData%\SAU\logs\sau-service.log`

**3. 时钟偏差告警**

- 托盘图标变黄，日志中出现 `Clock drift detected`
- Agent 会暂停任务调度直到时钟恢复同步
- 检查本机时间是否准确，必要时同步 NTP

**4. Cookie 失效**

```bash
# 检查账号状态
sau-ops accounts list
sau-ops accounts recheck

# 重新登录（通过托盘菜单或 CLI）
# 托盘 → 平台登录 → 选择平台
```

**5. 浏览器内核未安装**

```bash
sau-ops browser install
# 或从离线包安装
sau-ops browser install --from browsers.zip
```

**6. 环境全面检查**

```bash
sau-ops doctor
```

该命令检查 Python 版本、SAU_HOME 目录完整性、cookies 权限、patchright 内核、biliup、pywin32、pystray/Pillow、配置状态，并列出所有发现的问题。

---

## 技术栈

| 组件 | 技术 |
|------|------|
| **编程语言** | Python 3.10–3.12 |
| **异步框架** | asyncio |
| **WebSocket** | websockets |
| **HTTP 服务器** | aiohttp（本地控制 API） |
| **浏览器自动化** | patchright（Playwright fork，反检测） |
| **Windows 服务** | pywin32（win32serviceutil） |
| **系统托盘** | pystray + Pillow |
| **本地存储** | SQLite（WAL 模式） |
| **安全** | Windows DPAPI（token 加密，机器级保护） |
| **环境管理** | uv（虚拟环境与依赖管理） |
| **打包** | Nuitka（Python → 原生编译） |
| **安装包** | Inno Setup |
| **Web 后端** | Flask（管理后台，可选） |
| **前端** | Vue.js（管理前端，可选） |
