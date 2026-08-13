# SAU 桌面客户端设计（打包 EXE / 系统服务 / 托盘 / opcgeo 对接）

> 状态：设计稿（未开发）
> 关联文档：
> - opcgeo 后端：`opcgeo/docs/SAU-Agent对接设计.md`
> - opcgeo-html 前端：`opcgeo-html/docs/detailed-design/15-sau-agent.md`

## 1. 目标与需求映射

| # | 需求 | 设计结论 |
|---|------|---------|
| 1 | 打包成 exe，防反编译 | Nuitka 编译为原生二进制 + Inno Setup 安装包 |
| 2 | sau CLI 功能保留 | CLI 入口不变，同步打包为 `sau.exe`，并新增 service/token 子命令 |
| 3 | 安装后成为系统服务 | `SAUAgentService` Windows 服务（pywin32 服务宿主），开机自启 |
| 4 | 有系统托盘 | 独立托盘进程 `sau-tray.exe`（用户会话），登录自启 |
| 5 | 托盘菜单：启停服务、各平台登录 | pystray 菜单；登录在用户会话拉起有头浏览器 |
| 6 | 发布任务由 opcgeo 下发 | WebSocket 长连接接收 `publish_task`，本地执行上传 |
| 7 | API token 认证，与 opcgeo 用户绑定 | opcgeo 侧生成 Agent Token（绑定 userId），客户端 DPAPI 加密存储 |
| 8 | WebSocket 长连接，opcgeo 下发任务 | 新建独立 Agent 核心（`sau_agent_pkg/`），支持注册/心跳/进度/结果/账号同步 |
| 9 | 一客户端一机器：机器绑定 + 换机绑定 | 机器码（硬件指纹 SHA-256 前 32 位 hex）随连接上报；双模式绑定：opcgeo 创建 Agent 时可预填机器码（预绑定），留空则首次连接自动绑定；换机由 opcgeo 侧解除绑定 |
| 10 | 不修改原代码（仅限本项目） | 本项目为第三方开发并持续维护，核心代码可能随上游升级更新；为便于平滑合并上游更新，采用**包装层**策略：不改任何原有 py 文件，新能力全部在包装层实现（见 §2.3） |

## 2. 总体架构

### 2.1 双进程模型

Windows 服务运行在 Session 0，**不能**显示托盘图标，也不能弹出可交互的浏览器窗口。因此采用双进程：

```
┌─────────────────────────────────────────────────────────┐
│  Session 0（系统服务）                                    │
│  SAUAgentService（sau-service.exe）                      │
│  ├─ WebSocket Agent 核心：与 opcgeo 保持长连接             │
│  ├─ 任务执行器：下载素材 → headless 上传 → 回报结果        │
│  ├─ 本地控制 API：http://127.0.0.1:5410（仅本机）         │
│  └─ cookies/tasks/logs 维护                              │
└──────────────▲──────────────────────────────────────────┘
               │ 本地 IPC（HTTP + 本机随机 token）
┌──────────────┴──────────────────────────────────────────┐
│  用户会话（登录后自启）                                     │
│  sau-tray.exe（系统托盘）                                  │
│  ├─ 托盘图标：连接状态（绿/黄/红）                          │
│  ├─ 菜单：启停服务、平台登录、账号管理、打开日志、设置       │
│  └─ 登录流程：拉起有头浏览器（patchright headed）扫码/登录  │
└─────────────────────────────────────────────────────────┘
               ▲
               │ wss:// 长连接（token 认证）
        ┌──────┴──────┐
        │   opcgeo    │  下发 publish_task / 接收结果
        └─────────────┘
```

### 2.2 职责划分

| 进程 | 职责 | 不做什么 |
|------|------|---------|
| sau-service | WS 长连接、任务执行（headless）、账号有效性定时检查、本地控制 API | 不弹 UI、不做有头登录 |
| sau-tray | 服务启停控制、触发登录（有头）、展示状态、首次绑定 token 向导 | 不直连 opcgeo WS（统一由服务持有） |
| sau.exe (CLI) | 现有全部 CLI 能力 + 运维子命令 | — |

> 登录为什么放托盘进程：扫码登录需要有头浏览器显示在用户桌面，服务会话（Session 0）做不到；登录产出的 cookie 文件由服务进程消费，二者共享 `cookies/` 目录。

> **包装层约束（仅限本项目）**：social-auto-upload 是第三方开发并持续维护的项目，核心代码可能随上游升级更新；opcgeo 与 opcgeo-html 无此限制。因此本项目采用**包装层**定位：不直接修改任何现有 py 文件，新能力全部建在上游代码之上；复用方式优先级见 §2.3（直接 import ＞ 运行时垫片 ＞ 复制修改副本，后者为最后手段）；`sau_agent.py` 不纳入新链路（其动态导入与 Nuitka 不兼容，见 §11）。

### 2.3 包装层定位与上游升级兼容（为什么不改原代码）

**用意**：保证上游核心代码升级时能**直接替换文件、平滑合并**，包装层与上游更新互不阻塞。

**包装方式优先级**（由高到低）：

1. **直接 import 复用**（首选）：包装层调用上游公开函数（`sau_cli` 的 `login_*/check_*/upload_*`、`resolve_runtime_home` 等），行为用参数控制（headless/account_name 等）；
2. **运行时垫片**（次选）：上游行为不满足（如文件根路径）时在 import 前改写模块属性（见 §3.2 `home_shim`），不落盘、不改源文件，上游更新后垫片自动继续生效；
3. **复制为新文件修改**（最后手段，尽量避免）：仅在前两者无法实现时使用；副本会随上游更新而失配，需手工合并 diff，必须在下表登记并最小化。

**包装层依赖的上游 API 面**（升级兼容清单，所有 import 集中收口在 `sau_agent_pkg/upstream_adapter.py` 单点隔离）：

| 依赖点 | 具体符号 |
|--------|---------|
| 登录/校验 | `login_douyin_account` / `login_kuaishou_account` / `login_xiaohongshu_account` / `login_tencent_account` / `login_youtube_account` / `login_bilibili_account`；对应 `check_*_account` |
| 上传 | `upload_video`（抖音）/ `upload_note`；`upload_kuaishou_video/note`；`upload_xiaohongshu_video/note`；`upload_bilibili_video`；`upload_tencent_video`；`upload_youtube_video`；`upload_baijiahao_video`；及对应 `*UploadRequest` dataclass |
| 路径约定 | `conf.BASE_DIR`、`resolve_runtime_home()`、`resolve_account_file()`（`cookies/{platform}_{account}.json`） |
| 运行依赖 | Python 3.10–3.12；pyproject 的 patchright/pywin32 等（包装层新增依赖另行声明，不并入上游 pyproject） |

**上游升级流程**：拉取上游新版本 → 直接覆盖上游文件（包装层文件不动）→ 跑 `sau-ops doctor` 与上表 API 面回归 → Nuitka 重新构建发布。若上游符号破坏性变更，在 `upstream_adapter.py` 内做适配，**修改永不反灌上游文件**。

## 3. 模块与目录设计

### 3.1 源码结构（全部为新增文件，不改动任何现有文件）

```
social-auto-upload/
├── sau_cli.py                  # 【现有，保持原样】打包为 sau.exe，功能不变
├── sau_agent.py                # 【现有，保持原样】仅作历史参考，不纳入服务链路
├── sau_ops.py                  # 【新增】统一运维 CLI 入口（打包为 sau-ops.exe，见 §7）
├── sau_agent_pkg/              # 【新增】服务内置 Agent 核心（独立实现，不 import sau_agent.py）
│   ├── upstream_adapter.py     # 上游 API 收口层：集中 import sau_cli/uploader，隔离上游变更（见 §2.3）
│   ├── core.py                 # WS 主循环：注册/心跳/重连（平台处理器采用静态注册表）
│   ├── machine.py              # 机器码采集（见 §8.1）
│   ├── config.py               # 配置加载/保存（config.json + DPAPI）
│   ├── dispatcher.py           # publish_task → 上传调度、进度上报（经 upstream_adapter 调上传函数）
│   ├── accounts.py             # 账号扫描、cookie 有效性检查、account_sync
│   ├── db_init.py              # sau.db 建表（local_tasks/result_queue，见 §5.4）
│   └── local_api.py            # 127.0.0.1:5410 本地控制 HTTP API
├── sau_service/
│   ├── service_host.py         # pywin32 服务宿主（win32serviceutil）
│   └── runner.py               # 服务模式入口（也可前台调试）
├── sau_tray/
│   ├── tray_app.py             # pystray 主程序
│   ├── login_flows.py          # 各平台有头登录（import sau_cli 的 login_* 函数）
│   ├── home_shim.py            # SAU_HOME 运行时垫片（见 §3.2）
│   └── assets/icon.ico
├── packaging/
│   ├── nuitka_build.py         # Nuitka 构建脚本（四个目标）
│   └── installer/sau.iss       # Inno Setup 安装脚本
```

### 3.2 运行时数据目录

安装目录（Program Files）只读，运行时数据统一放 `%ProgramData%\SAU\`：

```
%ProgramData%\SAU\
├── config.json          # server_url、agent_id、心跳间隔等（明文）
├── credential.bin       # agent_token（DPAPI 加密，按当前机器+SYSTEM 保护）
├── local_token.bin      # 本地控制 API 随机令牌（服务启动时生成）
├── cookies/             # {platform}_{account}.json（服务与托盘共享）
├── downloads/           # 任务素材下载目录（用完即删）
├── logs/                # sau-service.log / sau-tray.log / tasks/
├── db/sau.db            # sqlite：本地任务记录、断线补发队列
└── updates/             # 自动更新安装包下载、备份（见 §9.1）
```

> CLI 独立运行时（`sau.exe`，未安装服务模式）仍沿用安装目录内 `cookies/`，行为与现在完全一致。
>
> **SAU_HOME 运行时垫片（不修改 `conf.py` 的实现方式）**：`conf.py` 不能改动，因此服务/托盘/`sau-ops` 的新入口在 **import `sau_cli` 之前**，先执行 `import conf; conf.BASE_DIR = Path(SAU_HOME)`（`SAU_HOME=%ProgramData%\SAU`）。`sau_cli.resolve_runtime_home()` 在调用时读取该属性，此后所有经 `sau_cli` 函数的 cookie/文件路径即落到 `%ProgramData%\SAU`。这是运行时行为改写，不触碰任何源文件。

**垫片伪代码**（`sau_tray/home_shim.py`，`sau-ops`/服务入口共用）：

```python
SAU_HOME = Path(os.environ.get("SAU_HOME") or os.environ["ProgramData"]) / "SAU"

def apply_home_shim() -> None:
    """必须在 import sau_cli 之前调用；只改内存属性，不落盘不改源文件"""
    import conf                      # 上游模块，保持原样
    conf.BASE_DIR = SAU_HOME         # resolve_runtime_home() 调用时读取，此后路径全部指向 SAU_HOME
    for sub in ("cookies", "downloads", "logs", "db", "logs/tasks"):
        (SAU_HOME / sub).mkdir(parents=True, exist_ok=True)
```

## 4. 打包与防反编译设计

### 4.1 方案对比

| 方案 | 产物 | 防反编译能力 | 结论 |
|------|------|-------------|------|
| PyInstaller | pyc 打包 | 差：pyinstxtractor + decompyle3 可还原源码 | 不采用 |
| PyInstaller + PyArmor | pyc 加密 | 中：存在成熟脱壳工具 | 备选 |
| **Nuitka --standalone** | C 编译为机器码 | 高：无 pyc，逆向成本等同 C/C++ 程序 | **采用** |

### 4.2 Nuitka 构建要点

构建四个目标（`packaging/nuitka_build.py` 统一驱动）：

| 目标 | 入口 | 参数要点 |
|------|------|---------|
| `sau.exe` | `sau_cli.py`（**原文件，原样打包**） | `--standalone --onefile`（CLI 追求单文件） |
| `sau-ops.exe` | `sau_ops.py`（新增运维 CLI） | `--standalone --onefile` |
| `sau-service.exe` | `sau_service/service_host.py` | `--standalone --windows-console-mode=disable` |
| `sau-tray.exe` | `sau_tray/tray_app.py` | `--standalone --windows-console-mode=disable --enable-plugin=pyside6`（如用 Qt）或不加 |

通用参数：

```
--include-package=uploader --include-package=utils
--include-package-data=utils            # stealth.min.js
--include-data-files=conf.example.py=conf.example.py
--cache-mode=isolated                   # 加速重复构建
--python-flag=no_site,no_asserts        # 生产模式（no_asserts 去除断言暴露的信息）
--windows-icon-from-ico=packaging/installer/icon.ico
--company-name=... --product-name="SAU Agent" --file-version=... 
```

防逆向加固清单：

1. **不用 onefile 存放核心逻辑以外的敏感物**：服务/托盘用 standalone 目录模式，启动快且便于加密资源文件。
2. **字符串不落盘**：服务端地址、协议常量在代码中拼接；token 等敏感数据运行时 DPAPI 解密，不写日志。
3. **DPAPI 保护 token**：`win32crypt.CryptProtectData`，服务进程以 Local System 身份加密（`CRYPTPROTECT_LOCAL_MACHINE`），托盘读取同一机器级密文。
4. **日志脱敏**：task/heartbeat 日志不输出 token、cookie 内容。
5. **可选加固**（二期）：Nuitka `--obfuscation` 字符串加密（商业版）；对 `uploader/` 中平台选择器逻辑做变量名混淆。

### 4.3 浏览器内核（patchright）分发

patchright/playwright 浏览器内核体积大（数百 MB），**不打进 exe**：

- 安装包附带 `sau-ops browser install` 首次运行步骤（安装完成页自动执行）；
- 内核安装到 `%ProgramData%\SAU\browsers\`，通过 `PLAYWRIGHT_BROWSERS_PATH` 指向；
- 离线场景：提供内核离线压缩包 `browsers-{version}.zip`，`sau-ops browser install --from <zip>`。

### 4.4 安装包（Inno Setup）

`packaging/installer/sau.iss` 安装流程：

1. 复制 standalone 目录到 `%ProgramFiles%\SAU\`；
2. 执行 `sau-service.exe install`（注册服务，启动类型：自动(延迟启动)）；
3. 写入当前用户注册表自启：`HKCU\Software\Microsoft\Windows\CurrentVersion\Run\SauTray = "%ProgramFiles%\SAU\sau-tray.exe"`；
4. 创建开始菜单快捷方式；
5. 引导用户打开托盘 → “绑定 opcgeo 账号”（输入服务器地址 + Agent Token）；托盘同步展示本机机器码（`sau-ops machine-code`），首次连接成功后该机器与 Agent 绑定（见 §8.1）。

卸载流程：停止并删除服务 → 删除自启项 → 询问是否保留 `%ProgramData%\SAU`（cookie 数据）。

## 5. 系统服务设计

### 5.1 服务定义

| 项 | 值 |
|----|----|
| 服务名 | `SAUAgentService` |
| 显示名 | SAU Publish Agent |
| 账户 | Local System（headless 浏览器需要网络+本地文件权限） |
| 启动类型 | 自动（延迟启动） |
| 失败恢复 | 第 1/2 次失败重启（延迟 10s），之后 60s |

### 5.2 服务宿主实现（pywin32）

`sau_service/service_host.py` 使用 `win32serviceutil.ServiceFramework`：

- `SvcDoRun`：初始化日志 → 加载配置 → 启动 asyncio 事件循环运行 `SAUAgent.start()` 与 `local_api`（同一 loop）；
- `SvcStop`：置停止标志 → 通知 agent 发送 `offline` → 关闭 WS → 退出；
- 同时注册 Windows 事件日志源，便于 services.msc 排障。

**服务宿主骨架伪代码**：

```python
class SAUAgentService(win32serviceutil.ServiceFramework):
    _svc_name_ = "SAUAgentService"; _svc_display_name_ = "SAU Publish Agent"

    def SvcDoRun(self):
        apply_home_shim()                              # §3.2：先于一切 import sau_cli 前
        logging_setup(SAU_HOME / "logs/sau-service.log")
        self.stop = threading.Event()
        asyncio.run(self._main())                      # agent + local_api 同一 loop

    async def _main(self):
        agent = SauAgentCore(config.load())            # sau_agent_pkg/core.py
        api   = LocalApiServer(token_file=SAU_HOME/"etc/local_token.bin")
        await asyncio.gather(agent.run(self.stop), api.run(self.stop))

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.stop.set()
```

### 5.3 本地控制 API（服务 ↔ 托盘/CLI）

`http://127.0.0.1:5410`（仅监听回环地址），所有请求需带 `X-SAU-Local-Token`（服务启动时随机生成写入 `local_token.bin`，**ACL = SYSTEM + Administrators + 安装时当前用户**；托盘进程以安装用户身份运行，必须可读该令牌，若安装者非管理员则不能仅授权 Administrators）：

| 接口 | 说明 |
|------|------|
| `GET /status` | 服务状态、WS 连接状态、agent_id、版本、活跃任务数、账号摘要、clock_offset_seconds、clock_sync_status（normal/drifted，见 §8.5） |
| `POST /login` | 仅查询登录支持的平台列表（真正登录由托盘进程执行） |
| `POST /accounts/recheck` | 触发全量 cookie 有效性检查并上报 account_sync |
| `POST /config` | 更新 server_url / 绑定 token（写入 credential.bin） |
| `GET /config` | 读取非敏感配置（token 只返回是否已绑定） |
| `POST /reload` | 配置热重载（重连 WS） |

> 服务“启动/停止”不经过本地 API，托盘直接调用 `win32serviceutil.ControlService` / `StartService`（需要管理员权限时托盘触发 UAC 提权）。

### 5.4 遗留链路处置与本地数据库（sau.db）

**遗留链路（保留原文件，不纳入安装包，明确声明如下）**：

以下三个模块在新架构下职责已**完全被取代**，**不纳入安装包、新链路不 import、原文件不改动**：

| 遗留模块 | 取代方 | 说明 |
|----------|--------|------|
| `sau_backend.py`（Flask 本地 HTTP API） | `sau_agent_pkg/local_api.py`（端口 5410 本地控制 API） | 完全取代，新 API 覆盖所有原功能且增加 WS Agent 管控能力 |
| `db/createTable.py`（旧 SQLite 建表，`user_info`/`file_records` 表） | `sau_agent_pkg/db_init.py`（新 SQLite 建表，`local_tasks`/`result_queue` 表） | 完全取代，新表结构适配 WS Agent 任务模型（见下方"新本地数据库"） |
| `myUtils`（旧后端素材下载工具集） | `sau_agent_pkg/dispatcher.py` 下载逻辑 | 完全取代，dispatcher 内置素材下载→上传→清理全流程 |

> **三者处置原则**：原文件保留在源码仓库中供历史参考，但**不打入安装包**（Nuitka 构建与 Inno Setup 打包均排除）；新链路代码中**不得 import 这三个模块的任何符号**。

- `sau_cli.py` 与 `uploader/` 是新链路的复用基础（登录/上传），正常打包。

**新本地数据库** `%ProgramData%\SAU\db\sau.db`（SQLite，新增 `sau_agent_pkg/db_init.py` 建表，取代旧 `db/createTable.py` 的相对路径 `./database.db` 方案）：

| 表 | 字段 | 用途 |
|----|------|------|
| `local_tasks` | `task_id`(PK), `payload`(JSON), `status`(queued/running/success/failed), `run_at`, `attempts`, `created_at`, `updated_at` | 任务执行状态与本地定时（时钟偏差兜底，见 §8.5） |
| `result_queue` | `id`(PK AUTOINCREMENT), `task_id`, `payload`(JSON), `created_at` | 断线期间 `task_result` 补发队列，重连后逐条重放 |

## 6. 系统托盘设计

### 6.1 技术选型

`pystray` + `Pillow`（轻量、纯 Python、Nuitka 兼容好）。状态轮询本地 API `/status`（3s 一次）刷新图标。

### 6.2 图标状态

| 图标 | 含义 |
|------|------|
| 绿色 | WS 已连接 opcgeo |
| 黄色 | 服务运行中但 WS 未连接（未绑定 token / 网络断开重连中）；或时钟偏差过大（见 §8.5）；或 token 剩余有效期 ≤7 天（见 §8.6） |
| 红色 | 服务未运行 |

### 6.3 菜单结构

```
SAU Agent  ● 已连接
├── 服务
│   ├── 启动服务（服务停止时显示）
│   ├── 停止服务（服务运行时显示）
│   └── 重启服务
├── 平台登录
│   ├── 抖音        （douyin login，有头浏览器）
│   ├── 小红书      （xiaohongshu）
│   ├── 视频号      （tencent/shipinhao）
│   ├── 快手        （kuaishou）
│   ├── B站         （bilibili：弹出终端二维码 + qrcode.png）
│   ├── 百家号      （baijiahao）
│   └── YouTube     （youtube）
├── 账号状态        （弹窗列出 cookies/ 下账号及有效性）
├── 重新检查账号有效性
├── 绑定 opcgeo 账号…（首次配置向导：服务器地址 + Agent Token）
├── 打开日志目录
├── 关于 / 检查更新
└── 退出托盘        （不停止服务，服务继续后台收发任务）
```

### 6.4 登录流程（托盘发起）

1. 点击“平台登录 → 抖音”；
2. 托盘进程内 `asyncio.run(login_douyin_account(account_name))`（import `sau_cli.py` 的 `login_*` 函数，headed=True；import 前应用 §3.2 的 SAU_HOME 垫片）；
3. 登录窗口完成后 cookie 写入 `%ProgramData%\SAU\cookies\douyin_{account}.json`；
4. 托盘调用本地 API `/accounts/recheck`，服务检查有效性并通过 WS `account_sync` 上报 opcgeo；
5. opcgeo 侧 `biz_platform_account` 更新，前端发布页即时可见授权状态。

> 账号名规则：默认 `default`；托盘提供“登录新账号”时输入自定义账号名（同平台多账号）。

## 7. CLI 保留与扩展（只新增，不改 sau_cli.py）

**原 `sau` CLI 100% 保留，`sau_cli.py` 源码零改动**（因此无法、也不向其追加子命令）。所有新能力通过**新增独立入口** `sau_ops.py`（打包为 `sau-ops.exe`，随安装包分发并加入 PATH）提供：

```
sau-ops service install|uninstall|start|stop|restart|status   # 服务运维
sau-ops tray                                                  # 前台启动托盘（调试）
sau-ops bind --server <url> --token <agent-token>             # 绑定 opcgeo（等价托盘向导）
sau-ops status                                                # 打印服务/连接/账号/机器绑定摘要
sau-ops machine-code                                          # 显示本机机器码（换机绑定时核对用）
sau-ops accounts list|recheck|remove --platform X --account Y
sau-ops browser install [--from <zip>]                        # patchright 内核安装
```

`sau-ops` 需要执行上传/登录时，通过 `home_shim` + import `sau_cli` 内部函数实现，与托盘共用同一套逻辑；`sau.exe` 与 `sau-ops.exe` 并存，互不影响。

## 8. 与 opcgeo 的通信协议

### 8.1 机器码与机器绑定（一个客户端只能在一台机器认证）

**机器码生成**（`sau_agent_pkg/machine.py`，新增）：

```
machine_code = SHA-256(
    Windows MachineGuid（HKLM\SOFTWARE\Microsoft\Cryptography）
  + "|" + 系统盘卷序列号（GetVolumeInformation）
  + "|" + CPU ID（Win32_Processor.ProcessorId，wmic 优先、PowerShell Get-CimInstance 回退）
).hexdigest()[:32]
```

**采集伪代码**（`sau_agent_pkg/machine.py`）：

```python
def get_machine_code() -> str:
    guid = winreg.QueryValueEx(                       # MachineGuid
        winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"),
        "MachineGuid")[0]
    vol = ctypes.windll.kernel32.GetVolumeInformationW  # 系统盘卷序列号
    vol("C:\\", None, 0, byref(serial := c_ulong()), None, None, None, 0)
    cpu = get_cpu_processor_id()                      # CPU ID：wmic 优先，失败回退 PowerShell Get-CimInstance Win32_Processor
    # 任一组件（MachineGuid/卷序列号/CPU ID）采集缺失 → 直接抛异常 fail-fast，不静默拼空串
    return hashlib.sha256(f"{guid}|{serial.value}|{cpu}".encode()).hexdigest()[:32]
```

- 三者均不随应用重装变化，重装系统会改变（符合“换机/重装需重新绑定”预期）；
- 任一组件采集缺失直接抛异常（fail-fast），避免产生弱指纹；
- 机器码不含任何个人信息，可安全上报；本地可通过 `sau-ops machine-code` 与托盘“关于”查看；服务端入库前 trim+小写归一化（正则 `^[0-9a-fA-F]{32}$`）。

**DPAPI 凭证读写伪代码**（`sau_agent_pkg/config.py`）：

```python
def save_token(plain: str) -> None:      # 服务以 SYSTEM 身份写入（机器级保护）
    blob = win32crypt.CryptProtectData(plain.encode(), "sau", None, None, None,
                                       win32crypt.CRYPTPROTECT_LOCAL_MACHINE)
    (SAU_HOME / "credential.bin").write_bytes(blob)   # ACL: SYSTEM/Administrators/安装用户

def load_token() -> str | None:          # 托盘（安装用户）与 SERVICE 均可解密
    try:
        _, raw = win32crypt.CryptUnprotectData(
            (SAU_HOME / "credential.bin").read_bytes(), None, None, None, 0)
        return raw.decode()
    except Exception:
        return None                       # 未绑定/密文损坏 → 引导重新绑定
```

**绑定规则**（服务端判定逻辑见 opcgeo 设计文档 §4.3）：

| 场景 | 行为 |
|------|------|
| Agent 创建时已预填机器码（预绑定） | 首次连接机器码必须与预填值一致，不一致同样按机器码不匹配拒绝（close 4409） |
| Agent 未绑定机器（`machine_code` 为空） | 首次连接携带的机器码写入服务端，绑定完成 |
| 机器码一致 | 正常通过 |
| 机器码不一致 | 拒绝连接：先下发 `bind_rejected` 消息（含原因），再关闭连接（close code 4409） |
| 换机 | 用户在 opcgeo 前端对该 Agent 执行“换机绑定”→ 服务端清空 machine_code（可选同时重置 token）→ 新机器下次连接自动重新绑定 |

**客户端对 4409 的处理**：停止自动重连，托盘图标置黄并弹出提示“该 Token 已绑定其他机器，请在新机器的 opcgeo 后台执行换机绑定”；原机器的 credential 不自动清除（防止误删），提供托盘菜单“解绑本机”手动清除。

### 8.2 连接与认证

- 地址：`wss://{opcgeo-host}/opcgeo/agent/ws`（开发环境 `ws://127.0.0.1:8888/opcgeo/agent/ws`，与现有 `sau_agent.py` 默认值一致）；
- 认证：连接 URL 携带 `?agentId={agent_id}&machine={machine_code}`（agentId 可缺省：服务端可按 token 哈希反查，并在 `registered` 下发权威 `agent_id`，客户端写回 config.json）；**token 经 `Authorization: Bearer {agent_token}` 请求头传递，URL 不再携带 token**（防网关日志留痕）；
- opcgeo 校验 token（哈希比对）并确认绑定用户后回 `registered`，否则关闭连接（code 4401）；机器码不匹配关闭 code 4409；**token 过期且超宽限期：握手拒绝（HTTP 403）或在线会话下发 `token_expired` 后关闭 code 4410**（有效期体系见 §8.6）；
- token 失效/被吊销：服务端关闭连接（code 4403），客户端不再自动重连并置托盘为“未绑定”状态，提示用户重新绑定。

**WS 主循环伪代码**（`sau_agent_pkg/core.py`，基于 `websockets` 库）：

```python
async def run(self, stop_event):
    backoff = 2
    while not stop_event.is_set():
        token = config.load_token()
        if not token: await sleep(30); continue            # 未绑定：低频等待托盘绑定向导
        url = f"{cfg.server_url}?agentId={cfg.agent_id or ''}&machine={get_machine_code()}"
        try:
            async with websockets.connect(url, ping_interval=None,
                    extra_headers={"Authorization": f"Bearer {token}"}) as ws:  # token 经 Bearer 头，不入 URL
                await ws.send(json({"type": "register", "data": {
                    "agent_id": cfg.agent_id, "machine_code": get_machine_code(),
                    "version": APP_VERSION, "platforms": PLATFORMS.keys(),
                    "accounts": accounts.scan()}}))
                backoff = 2                                 # 连上即重置退避
                async for raw in ws:                        # 收消息主循环
                    await router.dispatch(json.loads(raw))  # publish_task→dispatcher；account_check→accounts
                # 并发任务：heartbeat 每 30s、result_queue 补发、dispatcher 队列（同一 loop 多 task）
        except ConnectionClosed as e:
            if e.code == 4409: return tray.alert("机器码不匹配")   # 停止重连，等换机绑定
            if e.code == 4410: return tray.alert("Token 已过期，请到 opcgeo 后台续期或重置")
            if e.code in (4401, 4403): return tray.alert("凭证失效，请重新绑定")
        await sleep(backoff); backoff = min(backoff * 2, 300)   # 指数退避，上限 5 分钟
```

### 8.3 消息协议（JSON，`{"type": ..., "data": {...}}`）

全端统一封套：服务端出站消息（registered/heartbeat_ack/publish_task/bind_rejected/file_renewed/token_expired）业务字段均在 `data` 内；服务端入站从 `data` 读取（兼容顶层旧格式降级）；客户端出站同样使用 `data` 封套。

**客户端 → opcgeo：**

| type | data 字段 | 说明 |
|------|----------|------|
| `register` | agent_id, machine_code, version, platforms[], accounts[] | 连接建立后首条；machine_code 用于机器绑定校验；响应的 `registered.pending_tasks` 恒为空数组（积压任务仅在连接建立时由服务端 Handler 下发一次） |
| `heartbeat` | agent_id, active_tasks, accounts[], clock_offset_seconds | 每 30s；clock_offset_seconds 为客户端计算的时钟偏差秒数（见 §8.5） |
| `task_progress` | task_id, stage, percent, message | stage: downloading/uploading/publishing |
| `task_result` | task_id, status(success/failed), error, publish_url | 终态回报；错误信息字段为 `error`（服务端旧字段 `error_message` 仅作兼容回退） |
| `account_sync` | accounts:[{platform_key, account_name, is_valid(bool), checked_at}] | cookie 变化/定时上报；客户端按此格式上报，服务端兼容映射为 authorized(1/0)/status(checked|unchecked) 入库 |
| `file_renew` | task_id | 素材签名 URL 过期时请求重签（服务端回 `file_renewed`，回显同一 task_id；重签逻辑当前为 TODO 占位） |
| `error_log` | timestamp, level, module, message, stack_trace | ERROR 级别日志实时上报（见 §9.2）；stack_trace 为堆栈摘要（前 500 字符），敏感信息已脱敏 |

**opcgeo → 客户端：**

| type | data 字段 | 说明 |
|------|----------|------|
| `registered` | agent_db_id, **agent_id（权威值，客户端写回 config.json）**, machine_bound, expire_at（毫秒，null=永久）, token_status(permanent/normal/expiring/grace/expired), message, pending_tasks[]（首包响应恒为空数组） | 注册成功；积压任务仅在连接建立时由服务端 Handler 下发一次 |
| `bind_rejected` | reason(machine_mismatch/disabled/revoked), message | 拒绝连接的原因（随后服务端关闭连接） |
| `token_expired` | expire_at, message | token 已过期且超宽限期；收到后服务端关闭连接（close 4410），客户端冻结调度并提示续期（见 §8.6） |
| `heartbeat_ack` | server_time, expire_at（毫秒，null=永久）, expired_warning | server_time 用于计算时钟偏差（见 §8.5）；宽限期内 expired_warning=true，提醒续期（见 §8.6） |
| `publish_task` | task_id, content_type(video/note), platform_key, account_name, material_id, file_url, media_urls[], cover_url, bgm, title, description, tags[], scheduled_at | 任务下发；video 用 file_url，note（图文）用 media_urls 多图 |
| `account_check` | platforms[] | 指令：重新校验 cookie 并回报 |
| `file_renewed` | task_id, file_url, media_urls[] | 响应 `file_renew`：回显 task_id + 重签后的素材下载地址（重签逻辑当前为 TODO 占位） |
| `upgrade_notice` | version, download_url, file_hash | 升级提醒（托盘弹通知）；file_hash 为安装包 SHA-256 校验值（见 §9.1） |

### 8.4 任务执行语义

1. 收到 `publish_task` → 立即回 `task_progress(downloading)`；
2. 下载素材：`content_type=video` 下载 `file_url`；`content_type=note` 下载 `media_urls[]` 全部图片；统一落到 `downloads/{task_id}/`，任务终态后清理；签名过期时发 `file_renew` 换取新地址；
3. 解析账号：优先 `account_name`，缺失则取该平台第一个有效账号；cookie 不存在/过期 → 直接 `task_result(failed, "cookie missing/expired")`；
4. `scheduled_at` 非空且晚于当前时间：**定时调度以 opcgeo 侧扫描器到点下发为准**，SAU 本地仅对 ≤5 分钟的时钟偏差做兜底（sqlite 持久化等待，防服务重启丢失，时钟同步机制见 §8.5），超期任务直接执行；
5. 经 `upstream_adapter` 调上游 `upload_*` 函数执行上传（headless=True，见下方调度器伪代码）；
6. 终态回报（服务端按 task_id 幂等处理）+ 清理下载文件；断线期间的 `task_result` 落 sqlite 队列（`result_queue`），重连后补发。

**调度器伪代码**（`sau_agent_pkg/dispatcher.py`，静态平台注册表——对上游 API 的依赖全部经 `upstream_adapter` 收口）：

```python
# 平台能力注册表（编译期确定，兼容 Nuitka；字段即 sau_cli 的 Request dataclass）
PLATFORMS = {
    "douyin":     Caps(video=(DouyinVideoUploadRequest, upload_video),
                       note=(DouyinNoteUploadRequest, upload_note),
                       check=check_douyin_account),
    "kuaishou":   Caps(video=(KuaishouVideoUploadRequest, upload_kuaishou_video),
                       note=(KuaishouNoteUploadRequest, upload_kuaishou_note),
                       check=check_kuaishou_account),
    "xiaohongshu":Caps(video=..., note=..., check=check_xiaohongshu_account),
    "bilibili":   Caps(video=(BilibiliVideoUploadRequest, upload_bilibili_video), note=None,
                       check=check_bilibili_account),
    "tencent":    Caps(video=(TencentVideoUploadRequest, upload_tencent_video), note=None, ...),
    "youtube":    Caps(video=(YouTubeVideoUploadRequest, upload_youtube_video), note=None, ...),
    "baijiahao":  Caps(video=(BaijiahaoVideoUploadRequest, upload_baijiahao_video), note=None,
                       check=check_baijiahao_account),  # 视频上传（图文随上游补齐）
}

async def execute(task: dict, progress) -> None:
    caps = PLATFORMS[task["platform_key"]]
    account = task.get("account_name") or accounts.first_valid(task["platform_key"])
    if not account or not await caps.check(account):
        return await report(task, "failed", "cookie missing/expired")
    dest = SAU_HOME / "downloads" / str(task["task_id"]); dest.mkdir(parents=True)
    files = await download(task, dest, progress)       # video→file_url；note→media_urls[]；过期走 file_renew
    req_cls, upload_fn = caps.video if task["content_type"] == "video" else caps.note
    req = req_cls(account_name=account, title=task["title"], tags=task.get("tags", []),
                  video_file=files[0] if ... else None, image_files=files,
                  publish_date=None,                    # 定时由 opcgeo 控制，不透传平台定时
                  headless=True, debug=False)
    await upload_fn(req)                                # 成功即返回；异常分类后上报
    await report(task, "success")
```

> 平台并发由 `asyncio.Semaphore(max_concurrency)` 控制（默认 1，见 §11）；每个任务在 `local_tasks` 表先落 `queued` 再执行，服务重启后可恢复未完成任务。

### 8.5 时钟同步与偏差处理

客户端与服务端通过心跳机制维持时钟同步，确保定时任务（`scheduled_at`）准确执行。

**时钟偏差计算**：

客户端每次收到 `heartbeat_ack` 时，取其中的 `server_time` 字段（毫秒时间戳），与本地时间计算偏差：

```
offset_ms = server_time - System.currentTimeMillis()    # 正数表示本地时钟偏慢，负数表示偏快
```

偏差值持续更新（每次心跳都重新计算），取最近 N 次的滑动平均值作为当前 `clock_offset_seconds`。

**偏差处理策略**：

| 偏差范围 | 状态 | 行为 |
|----------|------|------|
| \|offset\| ≤ 5 分钟 | ✅ 正常 | 正常运行；定时任务使用修正后的时间（`local_time + offset`）判断是否到点执行 |
| \|offset\| > 5 分钟 | ⚠️ 告警 | **所有功能失效**——客户端不再执行任何任务（不上传、不定时），仅将接收到的任务缓存到 `local_tasks` 表（status=queued） |

**告警状态下的具体行为**（\|offset\| > 5 分钟）：

1. 托盘图标置**黄色**（无论 WS 连接状态）；
2. 弹出系统告警通知："系统时钟偏差过大，任务已暂停执行，请校准系统时间"；
3. WS 连接保持正常（心跳继续），`heartbeat` 消息中携带 `clock_offset_seconds` 字段上报当前偏差；
4. 收到的 `publish_task` 照常写入 `local_tasks`（status=queued），但不调度执行；
5. `task_result` 不发送（任务未执行）；
6. `/status` 本地 API 返回 `clock_offset_seconds` 和 `clock_sync_status`（`normal` / `drifted`）字段，托盘可展示偏差值。

**时钟恢复**（\|offset\| 回到 ≤ 5 分钟）：

1. 托盘图标恢复正常颜色；
2. 自动扫描 `local_tasks` 表中 status=queued 的任务，按 `created_at` 排序依次执行；
3. 恢复正常的定时调度与上传流程。

**伪代码**（时钟同步模块，集成在 `sau_agent_pkg/core.py` 心跳处理中）：

```python
CLOCK_DRIFT_THRESHOLD = timedelta(minutes=5)

def on_heartbeat_ack(self, server_time_ms: int) -> None:
    local_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    offset_ms = server_time_ms - local_ms
    self.clock_offset = self._sliding_avg(offset_ms)   # 滑动平均（毫秒）
    if abs(self.clock_offset) > CLOCK_DRIFT_THRESHOLD:
        self.clock_drifted = True
        self.dispatcher.pause()                            # 暂停所有任务调度
        tray.warn_clock_drift(self.clock_offset)           # 托盘告警
    elif self.clock_drifted and abs(self.clock_offset) <= CLOCK_DRIFT_THRESHOLD:
        self.clock_drifted = False
        self.dispatcher.resume()                           # 恢复调度，扫描 queued 任务
        tray.clear_clock_drift()

def corrected_now(self) -> datetime:
    """返回修正后的当前时间（供定时任务判断用）"""
    return datetime.now(timezone.utc) + self.clock_offset
```

### 8.6 Token 有效期（客户端行为）

有效期权威校验在 opcgeo 服务端（配置 `sau.token.valid-days: 30`、`sau.token.grace-days: 3`；握手超过 expire_at+宽限期 → HTTP 403，在线会话超宽限期 → 下发 `token_expired` 后 close 4410）；客户端校验仅作 UX，不作为安全边界：

1. **剩余天数计算**：以 `registered`/`heartbeat_ack` 携带的 `expire_at`（毫秒，null=永久）为准，基于 `server_time` 校正后的时间计算（见 §8.5），避免本地时钟不准误判；
2. **token_status 五态**：`permanent`（永久）/ `normal`（正常）/ `expiring`（剩余 ≤7 天）/ `grace`（宽限期内）/ `expired`（已过期）；
3. **宽限期内**：`heartbeat_ack` 携带 `expired_warning:true`，连接与任务执行保持正常，托盘提醒用户尽快续期；
4. **超宽限期**：客户端冻结调度（不执行新任务），收到 `token_expired`/close 4410 后停止重连并提示；
5. **托盘提醒**：剩余有效期 ≤7 天托盘图标置黄（见 §6.2）；
6. **续期途径**：opcgeo 后台 reset-token / rebind（均重置 expire_at）或续期接口（仅延期不换 token）；续期后客户端无需任何操作，下次心跳/重连自动生效。

## 9. 安全设计汇总

| 面 | 措施 |
|----|------|
| 代码保护 | Nuitka 原生编译；不打 pyc；生产构建去 assert |
| 凭证保护 | agent_token DPAPI 机器级加密；本地 API 随机 token + ACL；日志脱敏 |
| 机器绑定 | 硬件指纹机器码（任一组件缺失 fail-fast）；一 token 一机器；双模式绑定（预填预绑定/首次连接自动绑定）；换机需 opcgeo 后台显式操作；拒绝连接码 4409；token 过期超宽限期 4410 |
| 传输安全 | 生产强制 WSS；token 仅经握手 `Authorization: Bearer` 头传递（不经 URL，防网关日志留痕），不入日志 |
| 本地攻击面 | 本地 API 仅 127.0.0.1；cookies 目录 ACL 收紧（SYSTEM/Administrators） |
| 越权 | 客户端只执行服务端下发任务；不接受任意 URL 命令（file_url 域名白名单校验） |
| 日志上报 | ERROR 级别日志经 WS 实时上报 opcgeo（新增 `error_log` 消息类型，见 §9.2）；上报前脱敏处理（token/cookie 等替换为 `***`） |

### 9.1 客户端自动更新设计（半自动方案）

采用**半自动更新**策略：服务端推送升级通知 → 客户端自动下载安装包 → 用户确认后执行更新。兼顾自动化与用户控制权。

**更新流程**：

```
服务端推送 upgrade_notice（含 version / download_url / file_hash）
        │
        ▼
托盘收到通知 → 后台下载新版本安装包
        │      目标：%ProgramData%\SAU\updates\sau-{version}.exe
        │
        ▼
下载完成 → SHA-256 校验（对比 file_hash）
        │
        ├── 校验失败 → 丢弃安装包，日志记录，等待下次推送
        │
        ▼
校验通过 → 托盘弹出通知：“发现新版本 X.X.X，是否立即更新？”
        │
        ├── 用户确认 → 执行更新流程（见下）
        │
        └── 用户取消 → 延迟提醒（下次服务启动时再弹窗提醒）
```

**执行更新流程**（用户确认后）：

1. 备份当前版本：将 `%ProgramFiles%\SAU\` 复制到 `%ProgramData%\SAU\updates\backup\`；
2. 停止 `SAUAgentService` 服务（`net stop SAUAgentService`）；
3. 运行新版本安装包（静默安装模式 `/SILENT`），覆盖安装；
4. 启动 `SAUAgentService` 服务（`net start SAUAgentService`）；
5. 验证新版本正常运行（检查 `/status` 返回的版本号）；
6. 验证成功 → 清理备份；验证失败 → 自动回滚（从备份恢复，重启服务）。

**`upgrade_notice` 消息字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `version` | string | 新版本号（如 `"1.2.0"`） |
| `download_url` | string | 安装包下载地址 |
| `file_hash` | string | 安装包 SHA-256 校验值 |

**安装包存储**：

- 下载目录：`%ProgramData%\SAU\updates\`；
- 安装包命名：`sau-{version}.exe`；
- 更新完成后清理该目录下的旧安装包。

**回滚机制**：

- 备份保留策略：更新成功后保留备份 24 小时（便于发现问题后手动回滚）；
- 自动回滚触发：新版本服务启动后 60 秒内未响应 `/status` → 判定更新失败 → 自动从备份恢复并重启旧版本。

### 9.2 ERROR 日志自动上报

客户端 ERROR 级别日志通过 WS 通道实时上报 opcgeo 服务端，便于集中排障。

**上报机制**：

- 触发条件：客户端任意模块产生 ERROR 级别日志（`logging.error()` 及以上）；
- 传输通道：复用现有 WS 长连接，新增消息类型 `error_log`（见 §8.3）；
- 频率限制：同一模块同一错误消息 60 秒内仅上报一次（防刷屏）。

**上报内容**：

| 字段 | 说明 |
|------|------|
| `timestamp` | ISO-8601 UTC 时间戳 |
| `level` | 日志级别（`ERROR` / `CRITICAL`） |
| `module` | 模块名（如 `dispatcher`、`core`、`accounts`） |
| `message` | 错误消息 |
| `stack_trace` | 堆栈摘要（截取前 500 字符） |

**脱敏规则**：

上报前对 `message` 和 `stack_trace` 执行脱敏处理：
- token / cookie / password 等字段值替换为 `***`；
- 文件路径中的用户名部分保留（便于定位），但 cookie 文件内容不上报；
- 正则匹配常见敏感模式（Bearer token、JSON 中的 `token`/`cookie`/`secret` 键值）。

**服务端处理**：

- opcgeo 收到 `error_log` 后写入服务端日志文件（`logger.error("[SAU-REMOTE] agent={} module={} msg={}")`）；
- 可选：落库到独立表供前端查看（本期不实现，预留接口）。

## 10. 里程碑（开发阶段用）

| 阶段 | 内容 |
|------|------|
| M1 | `sau_agent_pkg` 新 Agent 核心（config/DPAPI/机器码/进度上报/账号同步）+ opcgeo 联调 |
| M2 | pywin32 服务宿主 + 本地控制 API；`sau-ops` 运维 CLI |
| M3 | 托盘（pystray）+ 登录流程 + 绑定向导（含机器码展示） |
| M4 | Nuitka 构建脚本（四目标）+ Inno Setup 安装包 + patchright 内核分发 |
| M5 | 机器绑定/换机流程联调、升级提醒与自动更新（§9.1）、时钟同步与偏差处理（§8.5）、ERROR 日志上报（§9.2）、断线补发、稳定性加固 |

## 11. 风险与约束

1. **Nuitka 对动态导入的兼容**：现有 `sau_agent.py` 的 `_run_upload_dynamic`（importlib 动态加载）与 Nuitka 不兼容且不能修改原文件，因此新链路不 import 它；`sau_agent_pkg/core.py` 采用**静态平台注册表**（直接 import 各 `uploader/*/main.py`），与 `sau_cli.py` 的调用方式保持一致。
2. **biliup 子进程**：bilibili 依赖外部 CLI，standalone 打包需将其作为数据文件包含或随内核分发。
3. **并发上限**：headless 浏览器同时跑多任务吃内存，默认并发 1，可配置（`config.json` 的 `max_concurrency`，建议 ≤3）。
4. **平台风控**：服务账户(Session 0)运行 headless 浏览器首次可能需要字体/权限初始化，安装脚本需做首次自检（`sau-ops doctor`）。
5. **Windows 服务更新**：更新 exe 需先停服务，升级流程由托盘或 `sau-ops service upgrade` 编排。
6. **机器码稳定性**：更换硬盘/重装系统会导致机器码变化，属预期行为（走换机绑定流程）；虚拟机克隆场景机器码可能相同，由服务端“同时在线互踢”策略兜底（同一 Agent 新连接挤掉旧连接）。
7. **上游升级破坏性变更**：social-auto-upload 为第三方持续维护项目，上游更新可能改动包装层依赖的符号（§2.3 API 面）；防线：所有 import 收口在 `upstream_adapter.py`，升级后先跑 `sau-ops doctor` 与 API 面回归再发布；**任何适配修改只在包装层做，永不改上游文件**。
