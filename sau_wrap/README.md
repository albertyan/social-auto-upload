# sau_wrap —— SAU 客户端包装层（S1 已落，S2：Agent WS 核心，S3：5409 本地 API，S4：任务执行核心，S5：瘦托盘，S6：本地 Web 控制台）

按《SAU客户端重建方案-单EXE与包装层设计》实施计划推进：
单一入口子命令分发 + pywin32 服务宿主 + **Agent WS 主循环（S2）** +
**5409 本地 API（S3）** + **任务执行核心（S4：dispatcher）** +
**瘦托盘（S5：pystray）** + **本地 Web 控制台（S6：Vue3 + 静态托管 + 票据/会话鉴权）**。
**上游源码零修改**（只新增本目录；控制台不复用上游 sau_frontend，冲突 1 定案）。

## 实现状态

| 能力 | 状态 |
| --- | --- |
| 子命令分发框架（Click） | ✅ `agent / service / tray / browser / doctor / machine-code / bind` |
| `service install` | ✅ 注册 + 延迟自启（DELAYED_AUTO_START）+ 失败梯度重启（30/60/120s，24h 重置）；重复安装（1073）友好提示 |
| `service remove/start/stop/status` | ✅（start 轮询窗口 60s，stop 等待 30s） |
| `service upgrade` | ⬜ 占位（S7） |
| `agent` | ✅ WS 主循环（S2）：注册/心跳/重连退避/凭证类关闭码挂起/任务落库/结果补发；服务异常以失败态退出触发 SCM 重启 |
| 任务执行核心（S4） | ✅ dispatcher：并发信号量（默认 2 可配）/账号解析/素材下载（403 重签）/上游上传器适配/异常分类/结果回报/重启恢复 |
| 5409 本地 API（S3） | ✅ `GET /status`、`GET/POST /config`、`POST /reload`、`POST /bind`；令牌鉴权；写操作审计；绑定失败明确报错（§4.4） |
| Web 控制台（S6） | ✅ 静态托管 `/ui/*` + 票据换 Cookie 鉴权 + Nonce 写防护 + 四页面（状态/绑定/账号/升级） |
| 登录/账号/升级端点 | ⬜ 占位 501（登录会话族 §6.5、升级 §7.4 留后续步骤） |
| 瘦托盘（S5） | ✅ `sau tray`：三菜单（打开控制台/打开日志目录/退出，无启停）+ `/status` 轮询（5s）+ 图标状态/气泡提示 + Mutex 单实例；「打开控制台」已接票据链路（S6） |
| `machine-code` | ✅ 真实机器码（SHA-256(MachineGuid+卷序列号+CPU ID) 前 32 位，§5.7） |
| `bind` | ✅ 写 `config.json` + `credential.bin`（DPAPI LOCAL_MACHINE） |
| `tray` | ✅ 瘦托盘（S5，见上） |
| `browser / doctor` | ⬜ 占位 |
| 版本号 | ✅ `version.py` 的 `APP_VERSION`（可被环境变量 `SAU_VERSION` 覆盖），`--version` 显示 |
| 日志 | ✅ `%ProgramData%\SAU\logs\service.log`（10MB × 5 轮转） |

## S2（Agent WS 核心）范围与语义

- **连接**：`{server_url}?agentId=&machine=`，Header `Authorization: Bearer <token>`，`ping_interval=None`（应用层心跳）；
- **注册**：首包 `register`（agent_id/machine_code/version/platforms/accounts），处理 `registered`；
- **心跳**：默认 30s（config.json `heartbeat_interval` 可配），含 `active_tasks`/`clock_offset_seconds`；
  `heartbeat_ack.server_time` 滑动窗口 10 样本平均估计时钟偏差，偏差 >5 分钟置调度暂停标志（本步仅标志位）；
- **重连**：指数退避 2s→300s，收到 `registered`（会话真正就绪）后复位；
- **挂起**：关闭码 4401/4403/4409/4410 → 挂起零重连，等待配置热重载唤醒（S3 由 `POST /config` 触发）；
- **任务**：`publish_task` 记日志 + 落 `local_tasks`（SQLite WAL；真实执行后续步骤）；
- **回执**：`task_result` 先落 `result_queue` 再发送、成功后删除；重连后补发积压；
- **停止**：响应服务停止事件，关闭连接后退出主循环；服务端主动 1000 关闭（如发版重启）走退避重连，仅停止信号才退出主循环；
- **补发**：`result_queue` 按批循环清空（每批间隔 0.5s），不受单次条数上限约束。

## S3（5409 本地 API）范围与语义（§3.5/§3.7/§4.4）

- **监听**：仅 `127.0.0.1:5409`（`config.json` 的 `local_api_port` 可覆盖）；与服务进程同进程并发运行（同一 asyncio 事件循环）；
- **绑定失败**：抛 `LocalApiBindError` 并记日志（提示端口可能被上游遗留 `sau_backend.py` 或其他进程占用），禁止静默失败；服务主体（WS 主循环）不受影响继续运行；
- **鉴权**：`X-SAU-Local-Token` 头；令牌**每次服务启动重新生成**写 `%ProgramData%\SAU\local_token.bin`（users 可读）；无/错令牌一律 401（`secrets.compare_digest` 防时序攻击）；
- **端点**：
  - `GET /status`：ws_connected / suspended / agent_id / version / active_tasks / accounts（骨架）/ clock_offset_seconds / scheduling_paused / token_expire_at / token_status（expired|suspended|unbound|ok）/ last_close_reason（字段结构按 §3.7 契约固定，托盘轮询项）；
  - `GET /config` / `POST /config`：读/写 server_url（可选 heartbeat_interval、local_api_port，端口变更重启生效）；未绑定时写入返回 409 引导先 bind；写入后触发热重载；
  - `POST /reload`：热重载——唤醒挂起态 / 断开当前连接以新配置重连（凭证类挂起后的人工恢复入口）；
  - `POST /bind`：复用 `sau bind` 同一逻辑（config.json + DPAPI 凭证）后热重载；
  - S6 起新增：`GET /ui/*`（静态托管）、`POST /ui-ticket`、`GET /ui/t/<ticket>`、`GET /nonce`、`GET /machine-code`（见下节）；
  - `/login`、`/accounts/*`、`/upgrade*`：占位 501 + 说明（登录会话族/升级留后续步骤）；
- **审计**：绑定/配置写入/热重载在 service.log 记一行 `[AUDIT] op=… source=127.0.0.1 result=… detail=… via=token|cookie`（S6 起附鉴权方式）。

## S4（任务执行核心）范围与语义（§5.3 流水线 / §4.5 素材重签）

- **接收与并发**：`publish_task` → dispatcher；`asyncio.Semaphore` 并发控制（默认 2，`config.json` 的 `max_concurrency` 可配）；幂等落库 `local_tasks`（queued→running→success/failed，INSERT OR REPLACE）；
- **重启恢复**：`recover_pending()` 扫 queued/running 重新入队（非法 JSON 标 failed）；由首次收到 `registered` 后幂等触发（`recover_pending_once`），保证 403 重签时 `file_renew` 有连接可发；已 success 任务重推直接跳过（防重复发布），failed 重推保留（服务端重试语义）；
- **账号解析**：扫描 `%ProgramData%\SAU\cookies\{platform}_{account}.json` 生成快照；指定 `account_name` 按名取，否则 first_valid；无可用账号 → failed 并写明原因（不自动重试）；本步 `is_valid` = 文件存在且合法 JSON（真实有效性复核留后续步骤）；
- **素材下载**：video 用 `file_url` 存 `downloads/{task_id}/video.mp4`，note 用 `media_urls[]`；aiohttp 512KB 分块；**403 → 上行 `file_renew{task_id}` → 等服务端回 `file_renewed`（新 URL）后重新下载**，重签上限 2 次防死循环；新 URL 回写 `local_tasks.payload`；
- **上游上传器**（import 方式，绝不修改上游）：
  | platform_key | content_type | 上游入口 |
  | --- | --- | --- |
  | douyin | video / note | `DouYinVideo.douyin_upload_video()` / `DouYinNote.douyin_upload_note()` |
  | kuaishou | video / note | `KSVideo.main()` / `KSNote.main()` |
  | xiaohongshu | video / note | `XiaoHongShuVideo.main()` / `XiaoHongShuNote.main()` |
  | bilibili | video | `run_biliup_command([...])`（默认分区 tid=21，to_thread 包裹） |
  | tencent | video | `TencentVideo.tencent_upload_video()`（唯一支持草稿：manual→is_draft） |
  | youtube | video | `YouTubeVideo.main()`（可映射但**未验证**） |
  | baijiahao | video | `BaiJiaHaoVideo.main()` |
- **异常分类**：cookie/登录态关键词 → failed 不自动重试（现状语义）；网络错误 → failed + 备注（服务端可按规则重投）；
- **结果回报**：复用 `result_queue` 语义（先落库再发送，断线补发）；完成后清理 `downloads/{task_id}`；manual 非草稿平台备注“平台不支持草稿，已直接发布”；
- **账号同步**：`account_sync` 上行——注册首包/心跳带 `scan_accounts()` 快照，任务结束后经 after_task_hook 再同步一次；
- **时钟保护**：`scheduling_paused`（时钟偏差 >5 分钟）或凭证过期时新任务保持 queued 不执行；
- **可测试性**：`upstream_adapter.register_uploader()` 注入假上传函数、`TaskDispatcher(downloader=…)` 注入假下载器；真实路径保留（惰性 import 上游）。
- **真实发布依赖**：需平台 cookie。cookie 由上游既有能力获取：`python sau_cli.py <platform> login`（浏览器扫码/登录），包装层不实现登录、只消费其产出。
  **cookies 目录位置差异处理策略**：上游默认写仓库内 `cookies/`（`conf.BASE_DIR`），包装层主目录为 `%ProgramData%\SAU\cookies\`——`accounts.py` 采取**双目录兼容扫描**：主目录优先，同名 `{platform}_{account}` 以主目录为准，上游目录只读回退（不修改不搬迁）；后续步骤可提供迁移/同步命令将常用账号收敛到主目录。

## S5（瘦托盘）范围与语义（设计文档第 5 章）

- **三菜单（定案，无启停）**：① 打开控制台（S6 起接真实链路：托盘持 `X-SAU-Local-Token` 调 `POST /ui-ticket` 换一次性票据 → 浏览器打开 `/ui/t/<票据>` 换 Cookie 会话，§6.3；票据获取失败回退打开 `/ui/` 并记日志）② 打开日志目录（`%ProgramData%\SAU\logs`，不存在则创建）③ 退出（仅退出托盘，不影响服务）。服务恢复全靠延迟自启 + 故障自动重启（§4.2）；
- **状态轮询**：每 5 秒（`SAU_TRAY_POLL_SECONDS` 可覆盖）调 `GET /status`，带 `X-SAU-Local-Token`（每轮重读 `local_token.bin`，服务重启换令牌自愈）；
- **状态矩阵**：在线（200 + `ws_connected` 且未挂起）→ 绿色图标；离线（200 但未连接/挂起）/401（令牌不匹配，通常服务刚重启）/不可达（服务未运行）→ 灰色图标；tooltip 含版本/连接态/活跃任务数；
- **离线提示**：正常→异常翻转时气泡「服务未运行，系统会自动恢复」（§5.2 定案措辞一字一致；挂起态走同一提示）；异常→正常再提示一次恢复；同态重复与首轮不提示；
- **单实例**：命名互斥量 `SAUTrayMutex`（§5.3），已存在直接退出并记日志。会话本地命名（不带 `Global\` 前缀）：全局命名空间需 SeCreateGlobalPrivilege，标准用户会话下创建会被拒；托盘每用户会话一个，无需跨会话。非 183 创建失败时报错退出，不误报「已在运行」；
- **日志**：`tray.log`（5MB × 3，§14.1）；托盘异常全部捕获记日志，与服务进程架构隔离，崩溃不影响服务；
- **权限**：普通用户运行——仅读 `local_token.bin`（users 可读）与写 `logs/`（users-full），全程无提权操作；开机自启注册表 `HKCU\...\Run\SAUTray` 由安装包写入（§5.4，属 S8 打包步骤，本目录不涉及）。

### S5 手动验证步骤（交互会话，自动验证无法覆盖）

1. 启动服务：`python -m sau_wrap agent run-fg`（另开终端）；
2. 启动托盘：`python -m sau_wrap tray` → 系统托盘出现灰色图标（未绑定/未连接），数秒后若服务已连接则变绿；悬停看 tooltip（版本/连接态/活跃任务）；
3. 右键菜单：「打开控制台」→ 浏览器经票据链路打开控制台（S6：`/ui/t/<票据>` 换会话后直达 `/#/`）；「打开日志目录」→ 资源管理器打开 `%ProgramData%\SAU\logs`；
4. 停掉服务（Ctrl+C）→ 约 5 秒内图标变灰 + 气泡「服务未运行，系统会自动恢复」；重启服务→ 图标变绿 + 气泡「服务已恢复在线」；
5. 再开一个 `python -m sau_wrap tray` → 提示已在运行并直接退出（tray.log 有记录）；
6. 「退出」菜单 → 托盘消失，服务不受影响；
7. （建议）在**标准用户会话**（非管理员）下重复步骤 2 与 5：验证托盘可正常启动（互斥量为会话本地命名，无 SeCreateGlobalPrivilege 依赖）与单实例语义。

## S6（本地 Web 控制台）范围与语义（设计文档第 6 章）

- **技术选型**：包装层自建 `sau_wrap/console/`（Vue3 + Vite + hash 路由，冲突 1 定案：不复用上游 `sau_frontend`）；原生样式无组件库，控制依赖面；前端只做「5409 本地 API 的浏览器皮肤」；
- **四页面**：状态总览（`/status` 全字段 5s 自动刷新）/ 绑定（`/machine-code` 展示 + `/config` 读写 + `/bind` + `/reload`）/ 账号（`/status` 快照展示，登录按钮置灰「登录功能建设中」）/ 升级（`/upgrade` 501 → 优雅展示「升级功能建设中」）；
- **静态托管**（§6.4）：`GET /ui/*` 从 `sau_wrap/console/dist`（打包后 `{app}\ui`，§6.7）提供；路径穿越防护；`index.html` no-cache / 资源长缓存；产物缺失返回友好提示页（含构建指引）；hash 路由无需服务端回退；
- **鉴权链路**（§6.3 定案，双鉴权并存）：
  - 托盘/CLI：`X-SAU-Local-Token` 头（既有机制不变）；
  - 控制台浏览器：`POST /ui-ticket`（需本地令牌）→ 一次性票据（`token_urlsafe(32)`、内存表、60 秒过期、单次核销）→ `GET /ui/t/<票据>` 核销种会话 Cookie（HttpOnly、SameSite=Strict）→ 302 `/ui/`；会话**单实例顶替**（新会话顶掉旧会话）+ 30 分钟无活动失效（服务重启会话失效可接受）；浏览器永不接触本地令牌；
- **写操作 Nonce 双重防护**（§6.3）：Cookie 会话的写操作（`/config`、`/bind`、`/reload`）必须先 `GET /nonce` 领取一次性 Nonce 经 `X-Console-Nonce` 头提交（一次性消费、短窗去重，缺失 403 / 重复 409）；令牌鉴权（托盘/CLI）无浏览器 CSRF 面，豁免；
- **401 统一拦截**：未认证浏览器式请求 → 401 引导页（从托盘「打开控制台」进入）；前端 fetch 封装拦截 401 → 跳引导视图，禁止失效态发起写操作；
- **审计**：控制台写操作沿用既有 `[AUDIT]` 行，新增 `via=token|cookie` 区分来源；
- **本步边界**：登录扫码会话链路（§6.5）与升级编排（§7.4）**不在本步**——`/login/*`、`/accounts/*`、`/upgrade*` 保持 501 占位，账号页仅展示快照、升级页优雅展示建设中；
- **构建与分发**（§6.7）：`cd sau_wrap/console && npm install && npm run build` → `dist/`；dist 与 node_modules **不入库**（`console/.gitignore`），打包时由 S8 构建链路经 Nuitka `--include-data-dir` 进安装包；
- **开发体验**（§6.8）：`npm run dev`（5173）+ vite.config.js 代理 API 到 127.0.0.1:5409（可设 `SAU_DEV_TOKEN` 由代理注入令牌头，仅限开发机）。

### 控制台访问方式（手动验证）

**方式一（推荐，托盘菜单）**：
1. 启动服务：`python -m sau_wrap agent run-fg`（另开终端）；
2. 构建控制台（首次）：`cd sau_wrap\console; npm install; npm run build`；
3. 启动托盘：`python -m sau_wrap tray` → 右键「打开控制台」→ 浏览器自动换会话并进入状态总览页；
4. 检查四页面：状态总览（5s 刷新）/ 绑定（机器码 + 表单）/ 账号（快照，登录置灰）/ 升级（建设中提示）；
5. 会话单实例：再点一次托盘「打开控制台」→ 新会话顶掉旧会话，旧标签页操作 → 401 引导页；
6. 写操作验证：绑定页保存/热重载 → 成功且 service.log 有 `[AUDIT] … via=cookie`。

**方式二（手动流程，排障用）**：
1. 读 `%ProgramData%\SAU\local_token.bin` 内容（管理员或服务账户）；
2. `curl -X POST -H "X-SAU-Local-Token: <令牌>" http://127.0.0.1:5409/ui-ticket` → 取 `ticket`；
3. 浏览器打开 `http://127.0.0.1:5409/ui/t/<ticket>`（60 秒内、仅一次）。

> 直接访问 `http://127.0.0.1:5409/ui/` 也可加载页面（静态资源公开），但任何业务请求会 401 → 前端展示引导页（属预期，验证 401 链路用）。

## 运行方式（开发环境，仓库根目录）

```powershell
# 依赖（当前环境已具备；新环境执行）
pip install -r sau_wrap\requirements.txt

# 帮助与版本
python -m sau_wrap --help
python -m sau_wrap --version

# 1) 查看机器码（报给管理端录入）
python -m sau_wrap machine-code

# 2) 绑定（管理端创建 Agent 后，用返回的 serverUrl 与一次性 token）
python -m sau_wrap bind --server wss://<host>/opcgeo/agent/ws --token sau_xxxx

# 3) 前台验证（未绑定时主循环等待绑定，不空转重连；Ctrl+C 退出）
python -m sau_wrap agent run-fg

# 4) 服务生命周期（需管理员权限）
python -m sau_wrap service install
python -m sau_wrap service start
python -m sau_wrap service status
python -m sau_wrap service stop
python -m sau_wrap service remove
```

打包形态下同一入口收敛为 `sau.exe`（SCM ImagePath 指向 `"…\sau.exe" agent --startup auto`）。

## 服务注册参数

- 服务名：`SAUAgentService`（显示名 `SAU Agent Service`）
- 启动类型：自动（**延迟启动**）
- 失败恢复：第 1/2/3 次失败分别于 30/60/120 秒后重启，24 小时重置失败计数
- ImagePath（源码形态）：`"<python>" "<repo>\sau_wrap\__main__.py" agent --startup auto`
- ImagePath（打包形态）：`"<{app}\sau.exe" agent --startup auto`

## 目录结构（§3.3）

```
sau_wrap/
├── __main__.py / entry.py     入口（python -m sau_wrap / sau.exe）
├── version.py                 版本单一事实源（SAU_VERSION 可覆盖）
├── paths.py / logutil.py      运行时数据布局（§3.6）与日志（§14.1）
├── agent/
│   ├── machine.py             机器码生成（§5.7）
│   ├── config.py              config.json + credential.bin（DPAPI，§5.8）+ bind
│   ├── db.py                  SQLite WAL：local_tasks / result_queue（§5.5）
│   ├── core.py                ClockTracker（时钟偏差滑动平均，§3.8 #1）
│   ├── ws_client.py           WS 主循环（S2 核心；S4：账号快照/上行辅助）
│   ├── dispatcher.py          任务执行核心（S4：流水线/重签/异常分类/恢复）
│   ├── accounts.py            账号快照扫描（双目录兼容，S4）
│   └── upstream_adapter.py    上游上传器适配层（平台×内容类型映射，S4）
├── service/                   host.py 服务宿主（asyncio 接线，含 dispatcher 挂载）；ops.py 服务管理；local_api.py 5409 本地 API（S3；S6：静态托管/票据/会话/Nonce）
├── tests/                     mock_ws_server.py + verify_s2/s3/s4/s5/s6.py（本地验证，不触碰上游）
├── tray/                      瘦托盘（S5：app.py 主体，pystray + Pillow 代码生成图标；S6：票据链路）
├── console/                   Web 控制台源码（S6：Vue3 + Vite；dist/node_modules 不入库，见 console/.gitignore）
├── upgrade/                   占位（S7）
├── packaging/                 占位（S8）
└── requirements.txt           包装层独立依赖清单（§8.1）
```

## 验证（mock 服务端，无需 opcgeo 后端）

```powershell
python sau_wrap\tests\verify_s2.py   # S2：WS 主循环，14/14 通过（结果写 tests\_verify_report.txt）
python sau_wrap\tests\verify_s3.py   # S3：5409 本地 API，14/14 通过（结果写 tests\_verify_report_s3.txt）
python sau_wrap\tests\verify_s4.py   # S4：任务执行核心，16/16 通过（结果写 tests\_verify_report_s4.txt）
python sau_wrap\tests\verify_s5.py   # S5：瘦托盘（模块级），29/29 通过（结果写 tests\_verify_report_s5.txt）
python sau_wrap\tests\verify_s6.py   # S6：本地 Web 控制台，29/29 通过（需先 npm run build；结果写 tests\_verify_report_s6.txt）
```

- **verify_s2**（五场景）：①注册握手 + 心跳往返 + publish_task 落库 + 优雅停止；②断线重连退避
（1011 → 实测间隔 2.02s/4.03s）；③4401 挂起零重连（6s 无新连接）+ 热重载唤醒重连；
④result_queue 离线积压（含 60 项多批）→ 补发 → 队列清空；⑤服务端主动 1000 关闭 →
退避重连不退出主循环。
- **verify_s3**（六场景）：①令牌文件生成；②401/200 鉴权 + /status 契约字段 + /config 读写（未绑定 409）+ /bind 落盘 + 占位 501（/login、/upgrade；/ui/* 自 S6 已实现）+ 审计日志；③4401 挂起 → `POST /reload` 唤醒重连；④端口占用 → `LocalApiBindError` + 明确日志；⑤退避可被热重载打断；⑥退避期间 /config 写入即时唤醒。
- **verify_s4**（五场景，全假注入不拉起上游）：①成功链路（落库→running→mock 上传→task_result success→downloads 清理→account_sync；含成功后重推不重复执行）；②403→file_renew→file_renewed 换 URL 重试成功；③cookie 错误分类 → failed 不重试（attempts=1）；④并发信号量（3 任务峰值并发=2）；⑤重启恢复（recover_pending 扫 queued/running 重新入队执行成功）。
- **verify_s5**（六场景，模块级不启动 GUI）：①状态轮询四态（在线/离线含挂起/401/不可达，mock /status）；②状态翻转与气泡触发（进入异常提示一次且措辞与 §5.2 一字一致、同态不重复、恢复再提示、首轮不提示）；③Mutex 单实例（会话本地命名；183→None；错误注入非 183 创建失败必须报错不得误报已在运行）；④日志轮转配置（5MB×3）；⑤控制台 URL/日志目录/tooltip 构造与令牌读回；⑥图标色块生成（绿/灰 + 状态→颜色映射）。真实托盘交互验证见上节「S5 手动验证步骤」。
- **verify_s6**（九组场景，需先 `npm run build`）：①构建产物存在性；②静态托管（/ui/ no-cache、资源长缓存、404、路径穿越编码/明文变体均拒绝）；③dist 缺失 → 503 友好提示页；④票据全链路（签发 401 拦截/60s/核销种 Cookie（HttpOnly+SameSite=Strict）→认证访问、单次核销、过期、托盘链路：build_ticket_url/死端口回退 None/持令牌换票据）；⑤单实例顶替（旧 Cookie 401 + 日志）；⑥会话超时；⑦401 引导页（浏览器式 HTML vs API JSON）；⑧Nonce 写防护（缺失 403/一次性/重复 409/令牌豁免/审计 via=）；⑨机器码端点。控制台真实交互验证见上节「控制台访问方式」。
- 数据隔离于 `tests\_tmpdata*`（`SAU_DATA_ROOT` 覆盖，不触碰 `%ProgramData%\SAU`）。
