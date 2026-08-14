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
│   ├── upstream_adapter.py #   上游 API 隔离层（平台能力注册表、上传/登录/检查函数映射；百家号本地适配）
│   ├── updater.py          #   在线更新状态机（upgrade_state.json）
│   └── __init__.py
├── sau_service/            # Windows 系统服务
│   ├── service_host.py     #   pywin32 服务宿主（Session 0，Local System；含 get_service_status / 事件日志）
│   ├── runner.py           #   前台调试模式入口（不依赖 pywin32）
│   └── __init__.py
├── sau_tray/               # 系统托盘应用
│   ├── tray_app.py         #   托盘图标、状态轮询、菜单构建（4 态启动/停止 enabled callable）
│   ├── login_flows.py      #   各平台有头登录流程
│   ├── home_shim.py        #   SAU_HOME 运行时垫片（改写 conf.BASE_DIR）
│   ├── services/           #   托盘侧业务服务
│   │   └── system_svc.py   #     提权服务控制（start/restart 前置 1060 自动补装服务）
│   └── __init__.py
├── packaging/              # 打包与安装
│   ├── nuitka_build.py     #   Nuitka 统一构建脚本（四个目标）
│   └── installer/
│       ├── sau.iss         #   Inno Setup 安装脚本（CurStepChanged 兜底 + 管理员自检 runas 重拉）
│       ├── post-install.bat#   安装后统一执行脚本（四层服务注册/启动兜底 + 15s 轮询 STATE=RUNNING）
│       ├── sau-diagnose.bat#   现场诊断脚本（sc query + 日志打包）
│       └── output/         #   生成的安装包（sau-x.y.z.exe）
├── dist/                   # 打包产物（Nuitka 输出 + 安装辅助脚本）
│   ├── sau-service.dist/   #   sau-service.exe（standalone 目录）
│   ├── sau-tray.dist/      #   sau-tray.exe（standalone 目录）
│   ├── sau.exe             #   上游 CLI（onefile）
│   ├── sau-ops.exe         #   统一运维 CLI（onefile）
│   ├── post-install.bat    #   打包后拷贝的安装后脚本
│   └── sau-diagnose.bat    #   打包后拷贝的诊断脚本
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
# 先完成 Nuitka 构建，然后执行（注意 ISCC 需要 Inno Setup 6，建议 6.7.3）：
#   重要：禁止用 UpdateResource 事后修改生成的 Setup.exe，会破坏 Inno Setup
#   尾部嵌入的 7z 数据容器 offset，触发 "The setup files are corrupted"。
& "D:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DVersion=0.1.3 "packaging\installer\sau.iss"
```

安装包产物位置：`packaging\installer\output\sau-<Version>.exe`

**管理员/UAC 保证方案（三重保险）：**
1. `[Setup] PrivilegesRequired=admin`（ISCC 默认写入 asInvoker manifest 失败时由后两层兜底）
2. `InitializeSetup` 阶段执行 `net session`（ExitCode 非 0 说明当前不是管理员），`ShellExec('runas', {srcexe})` 以提权方式重拉 setup
3. 服务安装/控制环节（见下）统一 `ShellExecuteW runas`，即便 setup 本身没提权也能逐项提权

> **注意（内置 Administrator 账户）：** Windows 安全策略默认对"内置 Administrator 账户"启用 `FilterAdministratorToken=0`（即 Admin Approval Mode 关闭），即使 manifest 写了 `requireAdministrator` 双击也会静默提权不弹 UAC。要测试弹 UAC 请使用普通用户账户，或 secpol.msc → 本地策略 → 安全选项 → "用户账户控制：用于内置管理员账户的管理员批准模式" → 启用 → 重启。

**安装包行为（含 0.1.3 后的增强）：**
1. 安装四个 EXE 及 standalone 依赖到 `{autopf}\SAU`
2. 创建 `%ProgramData%\SAU` 运行时数据目录（cookies、db、logs、downloads、browsers 等）
3. 写入当前用户自启注册表项（`HKCU\...\Run\SauTray`）
4. 安装 patchright 浏览器内核
5. 【服务注册 · 四层兜底（1060 防护）】确保 `SAUAgentService` 一定被 SCM 识别
   - ① post-install.bat Step2：幂等 remove → 两次 sau-service.exe install → 失败则 `sc create` 旁路
   - ② sau.iss `CurStepChanged(ssPostInstall)` Pascal 层再做一次 ① 的全流程（TStringList.LoadFromFile + Pos 解析 sc query）
   - ③ 托盘 [elevated_service_control](file:///d:/dev/workspace/social-auto-upload/sau_tray/services/system_svc.py#L87-L150)：用户点"启动/重启服务"时，若 `get_service_status` 含 "not installed"，会先提权 `sau-service.exe install` 再执行动作
   - ④ 以上全失败时 post-install.bat 在 `install.log` 打醒目 WARNING 横幅并 dump `sau-service-crash.log`
6. 【安装后自动启动服务】注册完成后不返回：双重 start（sau-service.exe start + `sc start` fallback）+ **15s 轮询 `sc query STATE`**，直到 `RUNNING` 才继续下一步（保证托盘自启后菜单"已启动/停止"状态正确）
7. 启动托盘应用
8. 创建开始菜单快捷方式（可选桌面快捷方式）
9. 卸载时询问是否保留 `%ProgramData%\SAU` 数据目录

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

### 上游任务下发协议（WS 上下行）

Agent 作为 **WebSocket 客户端**主动连服务端（URL：`config.json.server_url`，默认 `wss://…/opcgeo/agent/ws`，query 传 `agentId=<UUID>&machine=<机器码>`，HTTP header 带 `Authorization: Bearer <agent_token>`）。

#### 连接/注册流程
1. WS 握手成功后立即发 `type=register`（身份 + 能力 + 全量账号清单）
2. 服务端回 `type=registered`；若 `data.pending_tasks[]` 非空，则这些是设备离线期间积压的任务，Agent 会立即 submit 全部
3. 之后每 30s 发 `type=heartbeat`

#### 下行消息（服务端 → Agent）
| type | 说明 |
|---|---|
| `registered` | 注册成功回执：`{message, expire_at|null, agent_id, pending_tasks?:publish_task[]}` |
| `publish_task` | **下发一条发布任务**（见下完整 JSON 结构）→ `_handle_publish_task` 原封不动丢 Dispatcher.submit |
| `heartbeat_ack` | 心跳应答：`{server_time, expire_at, ...}`，Agent 用 server_time 做时钟同步（滑动平均） |
| `account_check` | 指令：立刻触发某平台某账号 cookie 检查 |
| `file_renewed` | 素材重签成功回执：`{task_id, file_url? | media_urls?[]}` → Agent 用新 URL 重新 submit 该任务 |
| `bind_rejected` | 绑定冲突：`{reason: replaced|rebind|token_reset}`，WS 正常关闭后 Agent 标记凭证无效不再重连 |
| `upgrade_notice` | 在线升级通知：`{version, download_url, sha256?, force?}`，写 `upgrade_state.json` 启动下载 |
| `token_expired` | token 已过期：Agent 进入冻结态，dispatcher.pause() 只入队不执行 |

#### 上行消息（Agent → 服务端）
| type | 触发时机 | data 关键字段 |
|---|---|---|
| `register` | 刚连上 WS 时 | `agent_id, machine_code, version, platforms[], accounts[{platform,account,valid}]` |
| `heartbeat` | 每 30s | `agent_id, active_tasks, accounts, clock_offset_ms` |
| `task_progress` | 任务阶段切换（下载/上传/发布） | `{task_id, stage, percent(0/50/70/100 典型), message}` |
| `task_result` | 任务最终成功/失败一次 | `{task_id, status:success|failed, error?, publish_url?}` |
| `file_renew` | 下载素材 403（签名过期）时抛 FileRenewNeeded → 自动发 | `{task_id, platform_key, content_type, file_url|media_urls}` |
| `account_sync` | 账号状态变化（登录新账号/删账号）时推一次 | `{accounts:[{platform,account,valid,last_check_at}]}` |
| `error_log` | 未归类致命错误上送 | `{task_id?, message, traceback_excerpt}` |

#### `publish_task.data` JSON 格式（服务端必须按此下发）

> Dispatcher.submit(data) 消费侧约定；所有未知字段会被忽略，核心必填字段缺省会直接进入 task_result=failed。

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `task_id` | ✅ | string | 全局唯一任务 ID（用于进度/结果关联、断线重放、file_renew 重签、幂等取消旧任务） |
| `platform_key` | ✅ | string | 平台 key：`douyin` / `kuaishou` / `xiaohongshu` / `bilibili` / `tencent` / `youtube` / `baijiahao`（见 [PLATFORMS 注册表](file:///d:/dev/workspace/social-auto-upload/sau_agent_pkg/upstream_adapter.py#L183-L220)） |
| `content_type` | ✅ | string | `"video"` 或 `"note"`（图文）；B 站/视频号/YouTube/百家号 **只支持 video**（Caps.note=None，下发会进 task_result=failed "该平台不支持 note 类型"） |
| `account_name` | ❌ | string | 指定上传账号；缺省 = 自动选 `accounts.first_valid(platform_key)` |
| **video 任务** | | | `content_type=video` 时使用： |
| `file_url` | ✅(video) | string | 视频下载 URL（通常是签名 CDN URL；过期返回 403 → 自动触发 file_renew 协议） |
| **note 任务** | | | `content_type=note` 时使用： |
| `media_urls[]` | ✅(note) | string[] | 多图素材下载 URL 数组（jpg/png/webp/gif，会下载为 media_000.jpg …） |
| **发布元数据** | | | 两类任务通用： |
| `title` | 推荐 | string | 标题；note 任务若 `title` 空串会 fallback 用 `description` |
| `tags[]` | 推荐 | string[] | 标签（上传时各平台自动转 `#xxx` 或写入平台标签接口） |
| `description` | 可选 | string | 描述/正文（note 任务还承担标题 fallback 角色） |
| `publish_date` | ❌ | 任意 | **注意当前 Dispatcher._build_request 会忽略该字段直接写 `None`（立发）**；定时发布需服务端在约定时间再下发该任务。 |

**两个真实下发示例：**
```json
// 视频任务（抖音）
{
  "type": "publish_task",
  "data": {
    "task_id":        "OPC-20260814-3a7f",
    "platform_key":   "douyin",
    "content_type":   "video",
    "account_name":   "dongchedage_official",
    "file_url":       "https://cdn.opcgeo.com/materials/OPC-20260814-3a7f.mp4?sign=xxx",
    "title":          "今天试车：比亚迪海豹 07 GT",
    "tags":           ["新能源","海豹","试驾","BYD"],
    "description":    "深圳国际赛车场测试，0-100 3.8s"
  }
}

// 图文任务（小红书）
{
  "type": "publish_task",
  "data": {
    "task_id":        "XHS-814-p1",
    "platform_key":   "xiaohongshu",
    "content_type":   "note",
    "media_urls": [
      "https://cdn.opcgeo.com/xhs/814-p1-cover.jpg?sign=xxx",
      "https://cdn.opcgeo.com/xhs/814-p1-2.jpg?sign=xxx",
      "https://cdn.opcgeo.com/xhs/814-p1-3.jpg?sign=xxx"
    ],
    "title":      "通勤穿搭｜5 套秋天不重样",
    "tags":       ["穿搭","通勤","OOTD"],
    "description":"身高 165 体重 48，单品链接在评论 🍂"
  }
}
```

#### 素材签名过期/重签的交互闭环
1. Agent 下载 file_url/media_url 命中 HTTP 403（签名过期）
2. Dispatcher 抛 `FileRenewNeeded` → 自动上送 `type=file_renew`（带 task_id + 原 URL）
3. 服务端重新签发 URL → 下行 `type=file_renewed`：`{task_id, file_url:NEW_URL}` 或 `{task_id, media_urls:[NEW1,NEW2,…]}`
4. Agent 收到后从 SQLite 里恢复该任务的完整 publish_task data，覆盖新 URL → **Dispatcher 重新 submit**（相当于幂等重试）

#### 冻结调度条件（下发的任务不会执行，只会 queued 入表）
1. **时钟偏差 > 5 分钟**（`CLOCK_DRIFT_THRESHOLD_MS = 300000`）：滑动平均偏差超限 → Dispatcher.pause()，偏差恢复自动 resume
2. **token 超期超 3 天宽限**：`heartbeat_ack.expire_at` + 3d 之后冻结，需服务端重新下发 `registered.expire_at` 或重新 bind token

### 各平台上传请求数据结构（UploadRequest Dataclass）

Dispatcher 根据 `publish_task.platform_key + content_type` 从 [PLATFORMS 注册表](file:///d:/dev/workspace/social-auto-upload/sau_agent_pkg/upstream_adapter.py#L183-L220) 取出对应 `(RequestClass, upload_fn)`，然后把 publish_task 字段映射成该平台的 **UploadRequest dataclass** 再调用上传。所有 dataclass 定义在 [sau_cli.py#L60-L188](file:///d:/dev/workspace/social-auto-upload/sau_cli.py#L60-L188)（百家号为本地适配，定义在 [upstream_adapter.py#L95-L104](file:///d:/dev/workspace/social-auto-upload/sau_agent_pkg/upstream_adapter.py#L95-L104)）。

#### 能力总览表

| platform_key | 视频 (video) | 图文 (note) | Dataclass 前缀 |
|---|---|---|---|
| `douyin` | ✅ | ✅ | DouyinVideoUploadRequest / DouyinNoteUploadRequest |
| `kuaishou` | ✅ | ✅ | KuaishouVideoUploadRequest / KuaishouNoteUploadRequest |
| `xiaohongshu` | ✅ | ✅ | XiaohongshuVideoUploadRequest / XiaohongshuNoteUploadRequest |
| `bilibili` | ✅ | ❌ | BilibiliVideoUploadRequest |
| `tencent` (视频号) | ✅ | ❌ | TencentVideoUploadRequest |
| `youtube` | ✅ | ❌ | YouTubeVideoUploadRequest |
| `baijiahao` | ✅ | ❌ | BaijiahaoVideoUploadRequest（本地适配） |

#### 通用常量说明

| 常量 / 枚举 | 取值 | 说明 |
|---|---|---|
| `publish_strategy` | `"immediate"` | （默认）立即发布 |
| `publish_strategy` | `"scheduled"` | 定时发布；此时 `publish_date` 不能传 `0/None`，按 `SCHEDULE_FORMAT = "%Y-%m-%d %H:%M"` 填 datetime |
| YouTube `visibility` | `"public"` (默认) / `"unlisted"` / `"private"` | 视频可见范围 |
| B站 `tid` | int（分区号，如 17=科技区、21=日常、138=搞笑） | B站视频必须指定分区 |
| `debug` | `True` (默认) | 上传器 debug 模式，会打更多内部日志 |
| `headless` | `True` (默认，YouTube 默认 False) | 是否无头浏览器；YouTube 因风控原因默认有头执行 |

> **映射约定（Dispatcher._build_request 规则，见 [dispatcher.py](file:///d:/dev/workspace/social-auto-upload/sau_agent_pkg/dispatcher.py)）：**
> - 所有 UploadRequest 的 `account_name`：若 publish_task 给了就用，否则 `accounts.first_valid(platform_key)` 自动找
> - `video_file` → 下载后的 `downloads/{task_id}/video.{mp4|mov|mkv}`
> - `image_files[]` → 下载后的 `downloads/{task_id}/media_000.jpg ...`（保持 media_urls 顺序）
> - `title` → publish_task.title；note 任务若 title 空 → fallback 用 description
> - `note` (图文正文字段) → publish_task.description；若缺 description → 用 title
> - `description` (视频描述字段) → publish_task.description；缺省为空串
> - `publish_date` → **当前 Dispatcher 忽略 publish_task.publish_date 直接写 `None/0`（立即发布）**；定时发布需要服务端按时间再下发
> - `tags[]` → 原样透传

---

#### 1. 抖音 douyin — DouyinVideoUploadRequest / DouyinNoteUploadRequest

**DouyinVideoUploadRequest**（视频）
[定义位置](file:///d:/dev/workspace/social-auto-upload/sau_cli.py#L60-L75)

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `account_name` | str | (必填) | 账号名，对应 cookies/douyin_{account}.json |
| `video_file` | Path | (必填) | 本地视频文件路径（mp4/mov/mkv） |
| `title` | str | (必填) | 视频标题 |
| `description` | str | (必填) | 视频描述/简介 |
| `tags` | list[str] | (必填) | 话题标签（上传时自动补 # 前缀） |
| `publish_date` | datetime \| int | (必填) | 发布时间（int=0=立即；或 SCHEDULE_FORMAT 的 datetime） |
| `thumbnail_file` | Path \| None | None | 自定义封面缩略图（选填） |
| `thumbnail_landscape_file` | Path \| None | None | 横屏封面（选填，抖音横版专用） |
| `thumbnail_portrait_file` | Path \| None | None | 竖屏封面（选填，抖音竖版专用） |
| `product_link` | str | `""` | 商品橱窗挂载链接（选填） |
| `product_title` | str | `""` | 商品标题（选填） |
| `publish_strategy` | str | `"immediate"` | `"immediate"` / `"scheduled"` |
| `debug` | bool | `True` | 上传器 debug 日志 |
| `headless` | bool | `True` | 无头浏览器 |

**DouyinNoteUploadRequest**（图文笔记）
[定义位置](file:///d:/dev/workspace/social-auto-upload/sau_cli.py#L78-L89)

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `account_name` | str | (必填) | 账号名 |
| `image_files` | list[Path] | (必填) | 多张图路径（jpg/png/webp/gif，顺序即展示顺序） |
| `title` | str | (必填) | 笔记标题 |
| `note` | str | (必填) | 笔记正文（Dispatcher 用 publish_task.description 映射） |
| `tags` | list[str] | (必填) | 标签 |
| `publish_date` | datetime \| int | (必填) | 发布时间 |
| `publish_strategy` | str | `"immediate"` | 立即 / 定时 |
| `debug` | bool | `True` | debug |
| `headless` | bool | `True` | 无头 |
| `bgm` | str | `""` | 背景音乐 ID（选填，抖音图文支持 BGM） |

---

#### 2. 快手 kuaishou — KuaishouVideoUploadRequest / KuaishouNoteUploadRequest

**KuaishouVideoUploadRequest**（视频）
[定义位置](file:///d:/dev/workspace/social-auto-upload/sau_cli.py#L92-L103)

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `account_name` | str | (必填) | 快手账号 |
| `video_file` | Path | (必填) | 视频路径 |
| `title` | str | (必填) | 标题 |
| `description` | str | (必填) | 描述 |
| `tags` | list[str] | (必填) | 标签 |
| `publish_date` | datetime \| int | (必填) | 发布时间 |
| `thumbnail_file` | Path \| None | None | 封面缩略图（选填） |
| `publish_strategy` | str | `"immediate"` | 立即 / 定时 |
| `debug` | bool | `True` | debug |
| `headless` | bool | `True` | 无头 |

**KuaishouNoteUploadRequest**（图文）
[定义位置](file:///d:/dev/workspace/social-auto-upload/sau_cli.py#L106-L116)

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `account_name` | str | (必填) | 账号 |
| `image_files` | list[Path] | (必填) | 多图路径 |
| `title` | str | (必填) | 标题 |
| `note` | str | (必填) | 正文 |
| `tags` | list[str] | (必填) | 标签 |
| `publish_date` | datetime \| int | (必填) | 发布时间 |
| `publish_strategy` | str | `"immediate"` | 立即 / 定时 |
| `debug` | bool | `True` | debug |
| `headless` | bool | `True` | 无头 |

---

#### 3. 小红书 xiaohongshu — XiaohongshuVideoUploadRequest / XiaohongshuNoteUploadRequest

**XiaohongshuVideoUploadRequest**（视频笔记）
[定义位置](file:///d:/dev/workspace/social-auto-upload/sau_cli.py#L119-L130)

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `account_name` | str | (必填) | 小红书账号 |
| `video_file` | Path | (必填) | 视频路径 |
| `title` | str | (必填) | 笔记标题（小红书标题必填，20字内曝光好） |
| `description` | str | (必填) | 笔记正文 |
| `tags` | list[str] | (必填) | 话题标签（小红书支持「参与话题」） |
| `publish_date` | datetime \| int | (必填) | 发布时间 |
| `thumbnail_file` | Path \| None | None | 视频封面（选填） |
| `publish_strategy` | str | `"immediate"` | 立即 / 定时 |
| `debug` | bool | `True` | debug |
| `headless` | bool | `True` | 无头 |

**XiaohongshuNoteUploadRequest**（图文笔记）
[定义位置](file:///d:/dev/workspace/social-auto-upload/sau_cli.py#L133-L143)

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `account_name` | str | (必填) | 账号 |
| `image_files` | list[Path] | (必填) | 多图（小红书最多 18 张；建议 3:4 竖图 1080×1440） |
| `title` | str | (必填) | 笔记标题（必填，否则上传接口失败） |
| `note` | str | (必填) | 正文 |
| `tags` | list[str] | (必填) | 话题标签 |
| `publish_date` | datetime \| int | (必填) | 发布时间 |
| `publish_strategy` | str | `"immediate"` | 立即 / 定时 |
| `debug` | bool | `True` | debug |
| `headless` | bool | `True` | 无头 |

---

#### 4. Bilibili — BilibiliVideoUploadRequest（仅视频）

[定义位置](file:///d:/dev/workspace/social-auto-upload/sau_cli.py#L146-L154)

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `account_name` | str | (必填) | B站账号 |
| `video_file` | Path | (必填) | 视频路径（biliup 上传，支持分片并发） |
| `title` | str | (必填) | 视频标题（80 字符内） |
| `description` | str | (必填) | 简介（250 字符内） |
| `tid` | int | (必填) | **分区号**（B站强制要求）：如 17=科技、21=日常、138=搞笑、188=科技-野生技能协会、234=影视杂谈 |
| `tags` | list[str] | (必填) | 标签（最多 10 个） |
| `publish_date` | datetime \| int | (必填) | 发布时间（0=立即；B 站定时发布需要大会员或粉丝数达标） |

> **注意：** B站 dataclass 没有 debug/headless 字段 —— 因为底层走 `biliup-rs` 命令行上传工具，不是 Playwright 浏览器，也没有无头/有头概念。也没有 publish_strategy 字段，由 publish_date=0/非 0 自动判断。

---

#### 5. 视频号 tencent — TencentVideoUploadRequest（仅视频）

[定义位置](file:///d:/dev/workspace/social-auto-upload/sau_cli.py#L157-L173)

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `account_name` | str | (必填) | 视频号绑定的微信号账号名 |
| `video_file` | Path | (必填) | 视频路径（建议 30 分钟内，4K H.264） |
| `title` | str | (必填) | 标题 |
| `description` | str | (必填) | 描述 |
| `tags` | list[str] | (必填) | 标签 |
| `publish_date` | datetime \| int | (必填) | 发布时间 |
| `thumbnail_file` | Path \| None | None | 通用封面（选填） |
| `thumbnail_landscape_file` | Path \| None | None | 横版 16:9 封面（选填） |
| `thumbnail_portrait_file` | Path \| None | None | 竖版 3:4 封面（选填） |
| `short_title` | str \| None | None | 短标题（视频号 13 字以内，首页卡片展示） |
| `category` | str \| None | None | 分类（视频号后台分类名，如「生活」「教育」） |
| `is_draft` | bool | `False` | 是否仅存草稿（不发布） |
| `publish_strategy` | str | `"immediate"` | 立即 / 定时 |
| `debug` | bool | `True` | debug |
| `headless` | bool | `True` | 无头 |

---

#### 6. YouTube — YouTubeVideoUploadRequest（仅视频）

[定义位置](file:///d:/dev/workspace/social-auto-upload/sau_cli.py#L176-L187)

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `account_name` | str | (必填) | YouTube 账号名（对应 cookie 文件） |
| `video_file` | Path | (必填) | 视频路径（建议 15 分钟以内，1080p+） |
| `title` | str | (必填) | 标题（100 字符内） |
| `description` | str | (必填) | 视频描述（5000 字符内，支持换行和链接） |
| `tags` | list[str] | (必填) | 标签（最多 500 字符合计） |
| `thumbnail_file` | Path \| None | None | 自定义缩略图（1280×720，<2MB） |
| `playlist` | str \| None | None | 发布后加入的播放列表名（选填；不存在不会自动创建） |
| `visibility` | str | `"public"` | 可见范围：`public`（公开）/ `unlisted`（不公开搜索）/ `private`（私有） |
| `debug` | bool | `True` | debug |
| `headless` | bool | `False` | **YouTube 默认有头**；因 Google 风控会检测无头模式，无头模式失败率很高 |

> **YouTube 风控注意：** `headless=False` 需要桌面会话存在（即服务 Session 0 不能跑 Playwright GUI），因此 YouTube 上传推荐手动在用户会话下用 `sau_service/runner.py` 前台模式跑，或者用有头 + Session 0 隔离。

---

#### 7. 百家号 baijiahao — BaijiahaoVideoUploadRequest（仅视频，本地适配）

[定义位置](file:///d:/dev/workspace/social-auto-upload/sau_agent_pkg/upstream_adapter.py#L95-L104)（百家号作为本地适配，不在上游 sau_cli.py 中）

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `account_name` | str | (必填) | 百家号账号 |
| `video_file` | Path | (必填) | 视频路径 |
| `title` | str | (必填) | 标题（百家号推荐 15-30 字含关键词） |
| `tags` | list[str] | (必填) | 标签（百家号标签越多分发越好） |
| `publish_date` | datetime \| int | (必填) | 发布时间 |
| `debug` | bool | `True` | debug（百家号本地适配默认开 debug） |
| `headless` | bool | `True` | 无头浏览器 |

> **百家号说明：** 百家号没有标准化的 login/check CLI 函数，登录/检查直接走 `uploader.baijiahao_uploader.main` 的 `baijiahao_setup()` + `cookie_auth()`。百家号 UploadRequest 没有 `description` 独立字段（底层 `BaiJiaHaoVideo` 构造时只取 title/tags/publish_date/file_path，description 会被底层自动从简介栏推断）。百家号也没有 `publish_strategy` 字段，由 publish_date=0/非 0 自动判断立即或定时。

### 任务调度

`Dispatcher` 负责任务的完整执行流程：

```
接收 publish_task
    ↓
落 local_tasks 表（status=queued）【进程重启靠 recover_pending() 读回来重跑】
    ↓
等待信号量（并发控制，默认 max_concurrency=1）
    ↓
解析账号（account_name 缺省时自动找 first_valid）→ 检查 cookie 有效性（过期直接 failed）
    ↓
下载素材（aiohttp 异步下载，video → downloads/{task_id}/video.mp4，note → media_000.jpg/…）
    ↓
【HTTP 403】→ 抛 FileRenewNeeded → 走 file_renew 协议 → 新 URL 到后重新 submit
    ↓
构建 UploadRequest（dataclass：video 走 Caps.video[0]；note 走 Caps.note[0]）
    ↓
调用 upstream_adapter 上传函数（video→Caps.video[1] / note→Caps.note[1]）
    ↓
回报进度/结果（task_progress 多阶段 → task_result 一次【先写 SQLite result_queue 再上送，断线重发】）
    ↓
清理 downloads/{task_id} 临时目录
```

**异常分类（最终 task_result.status=failed 时的 error 字段约定）：**
- `cookie missing for {platform} {account}` / `cookie expired`
- `Unknown platform: {platform_key}` / `Platform {p} does not support {content_type}`
- `Download failed: HTTP {status} {file_url}`（非 403 的下载失败；403 走 file_renew 不会直接失败）
- `Upload function not found for …`
- 其余未分类异常的 traceback 摘要（`{type}: {message}`）

**重启恢复：** 服务启动时 `recover_pending()` 扫描 `local_tasks` 中 `status in ('queued','running')` 的行，重新 Dispatcher.submit 并覆盖落库行为（不会重复 insert）。

### 账号管理

- **Cookie 存储：** `SAU_HOME/cookies/{platform}_{account}.json`
- **账号扫描：** 启动时扫描 cookies 目录，解析文件名获取平台与账号信息
- **有效性检查：** 通过 `upstream_adapter.check_fns` 调用各平台检查函数
- **平台登录：** 托盘菜单触发有头（headed）浏览器登录流程，用户扫码或手动登录后保存 Cookie

### 本地控制 API

监听 `127.0.0.1:5410`，所有请求需携带 `X-SAU-Local-Token` 请求头（值来自 `%ProgramData%\SAU\local_token.bin`，服务每次启动时随机生成；服务刚启动托盘先起会遇到 500 "Local token not configured"，属正常现象，几毫秒后 token 文件生成就恢复）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/status` | 服务状态（WS 连接、活跃任务、账号列表、时钟偏差、token 剩余天数、last_close_reason 等） |
| POST | `/login` | 返回支持的平台列表（含 supports_video / supports_note 标志位）；body 可选 platform+account 打追踪日志 |
| POST | `/accounts/recheck` | **异步**触发全量 Cookie 检查 → 立即返回 `{task_id, status:"queued"}`，不阻塞调用方（避免多账号 10-25s 导致托盘 HTTP timeout） |
| GET | `/accounts/status` | 账号检查结果：① `?task_id=xxx` 命中当前任务 → 返回进度/结果（running/done/error + accounts[] + checked_at）；② 无 task_id 有上次缓存 → 返回 cached；③ 从未检查过 → `scanned_only` scan() 扫出全量账号（is_valid=None，保证 UI 不显示空白） |
| POST | `/config` | 更新 `server_url` / 绑定 `token`（DPAPI 加密存 credential.bin）/ 写入 `agent_id`；若绑定了 server_url/token 但没给 agent_id，会自动 get_agent_id 生成 |
| GET | `/config` | 读取非敏感配置（`server_url` / `agent_id` / `heartbeat_interval` / `max_concurrency` / `token_bound`） |
| POST | `/reload` | 配置热重载（触发 SauAgentCore.reload_config → WS 断开重连 + 时钟/注册重算） |
| GET | `/upgrade` | 在线更新状态（upgrade_state.json 原始内容：phase / version / download_p / error 等；无则返回 `{}`） |

认证失败响应：
- `local_token.bin` 尚未生成（服务启动中）：`500 {"error":"Local token not configured"}`
- `X-SAU-Local-Token` header 值不匹配：`401 {"error":"Unauthorized"}`

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

**服务控制菜单（4 态动态文本 + 动态置灰，用 pystray MenuItem 的 `text=` + `enabled=` callable 实现，每次显示菜单重新求值）：**

| 服务实际状态 | 启动菜单项 | 停止菜单项 |
|---|---|---|
| 🟢 running（SCM STATE=RUNNING） | 文本「已启动」+ **不可用（置灰）** | 文本「停止服务」+ **可用** |
| 🔴 stopped / not installed（1060） | 文本「启动服务」+ **可用** | 文本「已停止」+ **不可用（置灰）** |
| restart 菜单始终可用；选择启动/重启时，若托盘检测到 `not installed` 会先提权自动执行 `sau-service.exe install` 完成注册再启动。

代码：[托盘菜单 4 态函数](file:///d:/dev/workspace/social-auto-upload/sau_tray/tray_app.py#L125-L158) + [elevated_service_control 自动补装](file:///d:/dev/workspace/social-auto-upload/sau_tray/services/system_svc.py#L87-L150)

**其他菜单功能：**
- 平台登录：抖音 / 快手 / 小红书 / B 站 / 视频号 / YouTube（百家号暂无登录菜单，需手动放 cookie 文件）
- 账号状态查看与重新检查（重新检查已改为后台异步任务，返回 task_id 后托盘轮询 GET `/accounts/status?task_id=xxx`，避免多账号 10-25s 阻塞 HTTP 超时）
- 绑定 opcgeo 账号向导（tkinter 输入框 → 调用本地 API POST `/config`）
- 打开日志目录
- 关于（显示 Agent ID、机器码、连接状态、token 剩余天数、时钟偏差等）
- 检查更新（托盘轮询 GET `/upgrade`，读 upgrade_state.json phase/version）
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

**1. 托盘图标显示红色（服务未运行 / 1060 服务未安装）**

托盘日志里若反复出现 `get_service_status query failed: (1060, 'GetServiceKeyName', '指定的服务未安装。')`，是 SCM 没识别到 `SAUAgentService`。**从 0.1.3 起四层兜底，按以下顺序逐级尝试：**

```bash
# ① 最简单：托盘菜单直接点"启动服务"。系统会先检测 not installed → 先提权 sau-service.exe install → 再 start。
# ② 手动 CLI：
sau-ops service install     # 先注册
sau-ops service start       # 再启动
sau-ops service status      # 确认 STATE=RUNNING

# ③ 若 install 失败（pywin32 静默失败），直接 SCM 旁路：
sc create SAUAgentService binPath= "\"C:\Program Files\SAU\sau-service.exe\"" start= auto DisplayName= "SAU Publish Agent" depend= RpcSs
sc config SAUAgentService start= delayed-auto
sc description SAUAgentService "社交媒体自动发布 Agent 服务（Session 0，含 WS 长连接 + 本地 5410 API）"
sc start SAUAgentService

# ④ 现场信息收集（生成诊断包）：
%ProgramData%\SAU\sau-diagnose.bat
```

安装阶段也有兜底：安装包会在 `post-install.bat` Step 2 + `sau.iss CurStepChanged` 两次执行服务注册流程；若均失败，安装日志 `install.log` 尾部会有醒目的 `****************** WARNING ******************` 横幅并附带 `sau-service-crash.log`。

**2. WS 连接失败（黄色图标）**

- 检查 `config.json` 中 `server_url` 是否正确
- 检查 Token 是否已绑定：`sau-ops status`
- 若 `status.token_status = "frozen"`（超宽限）或 `last_close_reason = replaced/rebind/token_reset`（bind_rejected 冻结），需重新执行绑定向导：`托盘 → 绑定 opcgeo 账号` 或 `sau-ops bind --server <ws> --token <t>`
- 检查网络是否可达（`ping / wscat 连 server_url`）
- 查看服务日志：`%ProgramData%\SAU\logs\sau-service.log`

**3. 时钟偏差告警**

- 托盘图标变黄，日志中出现 `Clock drift detected` 或 `status.clock_sync_status = "drifting"`
- Agent 会 `Dispatcher.pause()` 暂停任务调度（只入队 queued 不执行），直到偏差回到 5 分钟以内自动 resume
- 检查本机时间是否准确，必要时同步 NTP（`w32tm /resync /nowait`）

**4. Cookie 失效**

```bash
# 检查账号状态
sau-ops accounts list
# 注意：第一次点"账号状态"若显示 scanned_only（is_valid=null）表示从未做过检查，先触发一次：
sau-ops accounts recheck   # 会返回 task_id，后台异步执行，10-25s/账号

# 重新登录（通过托盘菜单或 CLI）
# 托盘 → 平台登录 → 选择平台（有头浏览器，用户扫码/手动登录后 Cookie 自动保存）
# 百家号暂无登录菜单，需按 cookies 命名约定（baijiahao_{account}.json）手动放置 cookie 文件
```

**5. 浏览器内核未安装**

```bash
sau-ops browser install
# 或从离线包安装（zip 解压后根目录含 CHROMIUM_VERSION 文件）
sau-ops browser install --from browsers.zip
```

**6. 安装包启动报 "The setup files are corrupted. Please obtain a new copy of the program."**

这是 **事后修改 Inno Setup Setup.exe 导致**：Inno Setup 的 Setup.exe = PE 头 loader + 尾部固定偏移的内嵌 7z/ZIP 数据容器，用 `UpdateResource` 写 `.rsrc`（比如嵌入 manifest）会改变节表大小/扇区对齐，使尾部数据 offset 表失效 → 完整性校验 100% 触发 corrupted。

**解决：** 不要用任何工具事后改生成的 `sau-x.y.z.exe`。
- 若需要管理员/UAC，安装包已内置 `InitializeSetup → net session ExitCode → ShellExec('runas', {srcexe})` 三重保险（见安装包章节）
- 重新执行 `ISCC.exe` 编译一份干净的安装包：
  ```powershell
  & "D:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DVersion=0.1.3 "packaging\installer\sau.iss"
  ```

**7. 双击安装包不弹 UAC（内置 Administrator 账户）**

这是 **Windows 安全策略默认行为**，不是代码 bug：Windows 对"内置 Administrator 账户（Administrator，SID S-1-5-21-...-500）"默认关闭 Admin Approval Mode（`FilterAdministratorToken=0`），即使 `requireAdministrator` manifest 也会静默提权不弹 UAC 确认框。

**如需真实弹 UAC 验证体验：**
- 用普通用户账户运行；或
- `secpol.msc → 本地策略 → 安全选项 → 用户账户控制：用于内置管理员账户的管理员批准模式 → 已启用 → 重启`

**8. 环境全面检查**

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
