# sau_wrap —— SAU 客户端包装层（S1~S8 已落，S9：登录扫码会话链路）

按《SAU客户端重建方案-单EXE与包装层设计》实施计划推进：
单一入口子命令分发 + pywin32 服务宿主 + **Agent WS 主循环（S2）** +
**5409 本地 API（S3）** + **任务执行核心（S4：dispatcher）** +
**瘦托盘（S5：pystray）** + **本地 Web 控制台（S6：Vue3 + 静态托管 + 票据/会话鉴权）** +
**半自动升级编排（S7：八态状态机 + 可注入执行器编排 + 启动自检自愈）** +
**打包与分发（S8：Nuitka 单 exe + Inno Setup + 哈希发布闭环）** +
**登录扫码会话链路（S9：登录会话管理器 + 上游登录运行时适配 + 账号端点族）**。
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
| 升级编排（S7） | ✅ `upgrade_notice` 校验（防投毒）+ 后台下载（边下边算 SHA-256/重试/单飞）+ 八态状态机持久化 + `/upgrade` 快照 + `/upgrade/apply`（可注入执行器编排 + 自动回滚）+ `/upgrade/snooze` + 启动自检三分支（§15.2）；真实停服/安装真机验证留待 S8 打包后 |
| 登录会话族（S9） | ✅ `POST /login/{platform}` 创建（二维码回调/单会话/5 分钟超时）+ 二维码/状态轮询 + 验证码注入 + 取消；抖音短信二验运行时桥接；成功后落盘→account_sync |
| 账号端点族（S9） | ✅ `GET /accounts/status`（主目录扫描+基础判定）、`DELETE /accounts`（删除+审计）、`POST /accounts/recheck`（一期文件级重扫，mode=file_scan；真实浏览器复核后续） |
| 瘦托盘（S5） | ✅ `sau tray`：三菜单（打开控制台/打开日志目录/退出，无启停）+ `/status` 轮询（5s）+ 图标状态/气泡提示 + Mutex 单实例；「打开控制台」已接票据链路（S6） |
| `machine-code` | ✅ 真实机器码（SHA-256(MachineGuid+卷序列号+CPU ID) 前 32 位，§5.7） |
| `bind` | ✅ 写 `config.json` + `credential.bin`（DPAPI LOCAL_MACHINE） |
| `tray` | ✅ 瘦托盘（S5，见上） |
| `browser install` | ✅ 内核安装（S8：npmmirror CFT 直链自管下载（完整内核+headless shell 两组件，zip 合计约 295MB：~173MB + ~114MB，任务 #26 实测镜像 ~8MB/s）+ 官方源 patchright 回退，共 3 轮/套接字 30s 卡死保护 + `--from-file` 离线；落 `%ProgramData%\SAU\browsers\`） |
| `doctor` | ✅ 十一项体检（0 + ①~⑩：运行形态/服务/端口/WS/传输加密/配置凭证/机器码/内核/数据目录可写/磁盘/日志末 20 行；有 FAIL 退出码 1） |
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
  - `GET /config` / `POST /config`：读/写 server_url（可选 heartbeat_interval、local_api_port，端口变更重启生效）；未绑定时写入返回 409 引导先 bind；写入后触发热重载；GET 已绑定时追加凭证回显：`token_present` 布尔，为 true 时附 `token` 明文（仅监听 127.0.0.1 + 令牌/会话鉴权前提下的回显设计）；
  - `POST /reload`：热重载——唤醒挂起态 / 断开当前连接以新配置重连（凭证类挂起后的人工恢复入口）；
  - `POST /bind`：复用 `sau bind` 同一逻辑（config.json + DPAPI 凭证）后热重载；
  - S6 起新增：`GET /ui/*`（静态托管）、`POST /ui-ticket`、`GET /ui/t/<ticket>`、`GET /nonce`、`GET /machine-code`（见下节）；
  - S7 起新增：`GET /upgrade`（只读快照）、`POST /upgrade/apply`（确认编排，写操作走 Nonce 链路）、`POST /upgrade/snooze`（见 S7 节）；
  - S9 起新增：`/login/*` 登录会话族与 `/accounts/*` 账号族（见 S9 节；`/accounts/recheck` 一期落地文件级重扫，mode=file_scan）；
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
- **四页面**：状态总览（`/status` 全字段 5s 自动刷新）/ 绑定（`/machine-code` 展示 + `/config` 读写 + `/bind` + `/reload`）/ 账号（`/status` 快照展示，登录按钮置灰「登录功能建设中」）/ 升级（S7 起接入真实快照与确认/暂缓按钮，见下节；S6 本步为 501 优雅展示）；
- **静态托管**（§6.4）：`GET /ui/*` 从 `sau_wrap/console/dist`（打包后 `{app}\ui`，§6.7）提供；路径穿越防护；`index.html` no-cache / 资源长缓存；产物缺失返回友好提示页（含构建指引）；hash 路由无需服务端回退；
- **鉴权链路**（§6.3 定案，双鉴权并存）：
  - 托盘/CLI：`X-SAU-Local-Token` 头（既有机制不变）；
  - 控制台浏览器：`POST /ui-ticket`（需本地令牌）→ 一次性票据（`token_urlsafe(32)`、内存表、60 秒过期、单次核销）→ `GET /ui/t/<票据>` 核销种会话 Cookie（HttpOnly、SameSite=Strict）→ 302 `/ui/`；会话**单实例顶替**（新会话顶掉旧会话）+ 30 分钟无活动失效（服务重启会话失效可接受）；浏览器永不接触本地令牌；
- **写操作 Nonce 双重防护**（§6.3）：Cookie 会话的写操作（`/config`、`/bind`、`/reload`）必须先 `GET /nonce` 领取一次性 Nonce 经 `X-Console-Nonce` 头提交（一次性消费、短窗去重，缺失 403 / 重复 409）；令牌鉴权（托盘/CLI）无浏览器 CSRF 面，豁免；
- **401 统一拦截**：未认证浏览器式请求 → 401 引导页（从托盘「打开控制台」进入）；前端 fetch 封装拦截 401 → 跳引导视图，禁止失效态发起写操作；
- **审计**：控制台写操作沿用既有 `[AUDIT]` 行，新增 `via=token|cookie` 区分来源；
- **本步边界**：登录扫码会话链路（§6.5）不在本步——`/login/*`、`/accounts/*` 保持 501 占位，账号页仅展示快照；升级编排 §7.4 由 S7 实施（见下节）；
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
> `GET /ui`（无尾斜杠）302 → `/ui/`；票据核销失败（无效/过期）对浏览器导航返回引导 HTML（按 Accept 判断，与 401 引导页同源逻辑），API 式请求返回 JSON。

## S7（半自动升级编排）范围与语义（重建方案 §3.8/§7.4/§15，现状文档 §7）

- **通知接收与校验（防投毒）**：`upgrade_notice` 三字段（version/download_url/file_hash）非空；`file_hash` 64 位小写 hex；版本语义化且**严格大于**当前 `APP_VERSION`（预发布版低于正式版：`2.0.0a0 < 2.0.0`）；`download_url` 强制 `https`；域名 ∈ 白名单——`config.json` 的 `update_domain_whitelist`（可显式配置），缺省回退 `server_url` 的 host 及其子域；不合法 → 记日志拒绝、`last_rejected` 落状态供端点展示，不进入下载；
- **后台下载**：落盘 `updates/sau-{version}.exe`；`.part` 临时文件 + 1MB 分块边下边算 SHA-256；失败 3 次尝试指数退避（2s→4s），哈希不符**不重试**（删 `.part` 回 `noticed` 等下次推送）；总超时 1800s；`asyncio.Lock` 单飞（并发触发仅一次真实下载）；成功后置 `ready` 并记 `installer_path`；
- **状态机八态**（现状文档 §7.3 原样保留）：`noticed → downloading → ready ⇄ snoozed → applying → success / failed / rolled_back`；下载失败/哈希不符回 `noticed`；持久化 `%ProgramData%\SAU\etc\upgrade_state.json`（tmp + `os.replace` 原子写），服务启动读回供自检与端点快照；
- **端点**（替换原 501 占位）：
  - `GET /upgrade`：只读快照（phase/目标版本/当前版本/下载进度/校验结果/错误/最近拒绝通知）；
  - `POST /upgrade/apply`：仅 `ready|snoozed` 可触发（其余 409 `upgrade_not_ready`）→ 置 `applying` → 后台线程执行编排（真实环境会停本服务，靠启动自检收敛终态）；
  - `POST /upgrade/snooze`：`ready → snoozed`（幂等），其余 409；
  - 两个写端点并入 Nonce 清单（Cookie 会话必须 `X-Console-Nonce`，令牌鉴权豁免）；
- **编排六步（SYSTEM 无 UAC，可注入执行器）**：`[1/6]` 停服等 30s → `[2/6]` robocopy 备份安装目录（`/E /PURGE`，exit≥8 判败并保留旧备份）→ `[3/6]` 杀托盘 → `[4/6]` 静默安装 `{installer} /SILENT /SUPPRESSMSGBOXES /NORESTART /LOG`（exit 0/1 为成功）→ `[5/6]` 幂等启服等 30s → `[6/6]` 轮询 `GET /status` 校验 `version==目标 && service_running`；
- **自动回滚**：任一步失败 → 停服 → robocopy 恢复备份 → 重启服务 → 二次校验 → `rolled_back`；回滚亦失败 → `failed` + 人工救援指引（`MANUAL_RESCUE_GUIDE`：备份目录位置/手动安装/日志路径）；控制台 `failed|rolled_back` 展示「下载安装包手动安装」兜底直链（§7.4）；
- **启动自检三分支**（§15.2）：读 `upgrade_state.json`，仅 `applying` 态触发——分支1：当前版本==目标 → 补做 `/status` 校验 → `success` + 清备份（校验未过保持 `applying` 待下次自检）；分支2：版本不符（半替换）且备份存在 → 自动回滚 → `rolled_back`；分支3：无备份 → `failed` + 人工救援指引；
- **启动清理**：删除 >24h 的备份目录与旧版本安装包（目标版本安装包保留）；
- **可测试性**：编排全部经 `UpgradeExecutor`（7 个可调用 + `resolve_install_dir`）注入——开发/测试用假执行器全路径覆盖；`real_executors()`（win32serviceutil 停启服 / robocopy / PowerShell CIM+taskkill 杀托盘 / Inno 静默安装 / urllib 轮询校验）仅在服务宿主接线；
- **真机验证缺口**：开发环境无服务注册与安装器，停服/安装/回滚全以假执行器覆盖；真实停启、Inno 安装、robocopy 备份恢复的真机验证留待 S8 打包出安装包后执行。

## S8（打包与分发）范围与语义（重建方案 §7/§8.5/§8.6/§8.7/§13/§17）

- **单一产物**：`sau_wrap/entry.py` → `sau.exe`（Nuitka --standalone，吸收全部子命令）；控制台窗口策略定案 `--windows-console-mode=disable`（GUI 子系统，托盘/服务无双击黑窗；终端调用时 stdout 继承调用方终端仍可见；致命异常由 `entry.py` AllocConsole 兜底弹窗）；
- **版本来源**：`SAU_VERSION` 环境变量 > `sau_wrap/version.py`（不回退读上游，§8.4）；**版本纪律（硬约束）**：正式发布版本禁带预发布后缀（`SAU_VERSION=x.y.z` 纯三段），开发基线可用 `a/b/rc` 后缀；
- **包含**：上游运行时 `uploader/utils/myUtils`（只读引用）+ `sau_wrap` 全量 + `patchright/playwright/aiohttp/websockets/pystray/PIL/win32` 等（biliup 不进包，见体积优化条）；`conf.example.py` + 控制台 `dist → ui/`（dist 缺失自动先构建）；patchright driver 经 `--include-package-data` 单份进产物；
- **体积优化（§8.6）**：`--nofollow-import-to` 排除清单 25 项（测试框架/tkinter/flask 系/流媒体遗留/trio/xhs/旧前端旧托盘 + 首构建 400MB 实测后追加裁剪：stream_gears 随 biliup 32.5MB，逐条注释依据见 `nuitka_build.py`）；**cv2/numpy 禁排除**（终审修复②：上游四大平台上传器模块顶层 `import cv2`，属隐性依赖必须随包，纪律固化于 `packaging/BUILD_ENV.md`）；`--disable-plugin=playwright`（防浏览器二进制进产物 +100MB，内核走 `browser install`）；biliup 不进包（上游经 subprocess 调独立二进制，首次使用按需下载）；`--python-flag=no_asserts`；**不用 UPX**（杀软误报风险定案）；目标体积 ≤100MB（不含内核，口径为 Inno 安装包）；
- **构建纪律（§7.5）**：`--jobs=4`、`CCACHE_DISABLE=1` + `--disable-cache=all`、`--remove-output` 清残留；编译器缺失由 Nuitka 自动下载（`--assume-yes-for-downloads`），版本落档 `packaging/BUILD_ENV.md`；
- **browser install（§8.7 三层方案）**：首选 npmmirror Chrome for Testing 直链自管下载（任务 #26 实测：`PLAYWRIGHT_DOWNLOAD_HOST` 的 playwright 镜像路径对新版内核 404，故改直链；两组件 = 完整内核 + headless shell，登录 headless 必需）；官方源经 patchright 驱动回退（`readline()` 无预读读行，防进度缓冲致误判卡死）；共 3 轮（直链→官方→直链），套接字读超时 30s 卡死保护；`--from-file <zip>` 离线安装（兼容两种 zip 布局）；内核统一落 `%ProgramData%\SAU\browsers\`（`entry.py` 顶部 `PLAYWRIGHT_BROWSERS_PATH` setdefault，SYSTEM 服务/托盘/CLI 全路径一致，避免默认 `%USERPROFILE%` 缓存跨会话不一致）；当前 chromium revision 1208（CFT 145.0.7632.6）；
- **安装器（sau.iss）**：固定 AppId（新布局自身，支撑覆盖升级 §8.4）；`PrivilegesRequired=admin`；LZMA2 ultra + SolidCompression（§8.6 手段⑤）；首装六步（[Files] 整目录释放 → post_install.bat：数据目录+users-full ACL → VERSION → 服务注册（失败 exit 11 不静默）→ 启动重试 3 次（失败 exit 12）→ 浏览器内核自动下载（任务 #26 决策变更：已装跳过/弱网 20 分钟上限/失败仅记日志不阻断，§7.3/§17 阶段 5）→ HKCU 自启（失败仅记日志，§17 阶段 6））；覆盖升级 `PrepareToInstall` 停服 + 句柄释放等待 30s；卸载六步（杀托盘/停服/remove+sc delete/删自启项/删程序文件/数据默认保留+勾选全删，§13）；产物命名 `sau-{version}.exe`（与升级链一致）；
- **哈希发布闭环（§8.5）**：`hash_release.py` 自动计算安装包 64 位小写 SHA-256，输出发布单 `release-manifest.json`（版本/文件名/哈希/下载地址占位）——运营只搬运不手填，管理端 `PUT /sau/upgrade-config` 按发布单填三字段即广播；
- **冻结形态判定**：统一走 `sau_wrap.paths.is_frozen()`（`"__compiled__" in globals()`：Nuitka 把 `__compiled__` 伪模块注入每个编译模块的模块级全局名，**不进 `sys.modules`**；兼容 `sys.frozen` 兜底）。**不可直接用 `sys.frozen` 或 `"__compiled__" in sys.modules`**：Nuitka standalone 产物不设 `sys.frozen`、伪模块也不在 `sys.modules`（实测 Nuitka 4.1.3），真机缺陷（2026-08-26）即因误判走源码分支构造 `python.exe + __main__.py` 的 ImagePath 导致服务注册失败；另 Nuitka standalone 的 `sys.executable` 指向产物内随附 `python.exe`（非 sau.exe），故冻结形态 ImagePath 一律取 `sys.argv[0]`（即 sau.exe 自身）构造 `"<sau.exe>" agent --startup auto`（SCM 直接拉起自身，无需 pythonservice.exe 代理）；`pythonservice.exe` 仍以 `--include-data-files` 随包同级兜底（pywin32 回退查找路径）；`sau doctor` 首项输出运行形态/ImagePath 诊断；
- **doctor 十一项（0 + ①~⑩，§14）**：运行形态（冻结判定 + ImagePath）/服务状态/5409 端口/WS 连通/WS 传输加密/配置凭证（越界时间戳安全降级）/机器码（诊断）/浏览器内核/数据目录可写/磁盘剩余（<2GB FAIL）/三日志末 20 行；单项外部调用 5s 守护；有 FAIL 退出码 1。
- **验证边界**：本步完成 Nuitka 产物验证（--help/--version/doctor/ui/index.html）与安装包编译实测（ISCC 6.7.3，产物 51.4MB ≤ §8.6 目标 100MB）；2026-08-26 真机缺陷修复（冻结判定改 `__compiled__` + pythonservice.exe 随包）后已重建产物并烟测；真实服务注册/启停/卸载/覆盖升级在干净虚拟环境（或提权）的验证留待下一步。

## S9（登录扫码会话链路）范围与语义（重建方案 §6.5/§3.5）

- **支持平台**：douyin / kuaishou / xiaohongshu / tencent（上游 `*_setup` 原生 `qrcode_callback`）；bilibili（biliup 交互终端）/baijiahao（`page.pause()` 人工介入）/youtube 不支持，创建即 400 附原因说明；
- **会话管理器**（`service/login_sessions.py`）：每平台单活跃会话（重复创建 409 携带既有 session_id）；状态机 `waiting → need_input（POST code 注入）→ waiting`，终态 `success/failed/timeout/cancelled`；总超时 300s（`asyncio.wait_for` 包裹，异常兜底 failed、取消 cancelled）；终态会话保留 600s 供收尾轮询后逐出；二维码回调兼容 data URL 与文件路径两种载荷；
- **可注入执行器**：管理器接受 `executor` 注入（verify_s9 全假注入，不真实打开平台页面）；默认 `default_real_executor` 分发四平台，参数 `handle=True, return_detail=True, qrcode_callback=cb, headless=True`，cookie 直写主目录 `%ProgramData%\SAU\cookies\{platform}_{account}.json`（`accounts.py` 双目录兼容扫描既有，登录成功后自动进快照与 account_sync）；
- **抖音短信二验运行时适配**（`service/login_adapt.py`）：上游抖音登录检测到短信二验输入框后仅记日志、等待有头浏览器中的手动输入，服务端 headless（Session 0）下不可用；本层以**运行时内存级适配**（不改上游源码）：会话期内替换 `dm._wait_for_douyin_login` 模块属性，复刻上游等待循环（登录完成判定/二维码失效刷新复用上游函数），短信输入框分支改为置 `need_input` → 等控制台 `POST /login/{sid}/code` 注入（单次窗口 120s，超时/会话终态 → 本次登录 failed）→ 填入并尝试提交（优先候选确认按钮「验证/确定/确认/登录/提交」，兜底回车）→ 继续等待登录完成；会话结束（含异常）还原原函数；另 `DOUYIN_COOKIE_AUTH_HEADLESS=true` 适配 Session 0 无桌面（上游原生开关）；
- **内核前置检查**：创建会话前检查浏览器内核，未装 → 503 `browser_missing` + 引导 `sau.exe browser install`；
- **端点**（替换原 501 占位，写操作并入 Nonce 清单，令牌豁免；GET 轮询不受约束）：
  - `POST /login/{platform}`：创建会话（body 可选 account_name）→ session_id/platform/status/expires_at；
  - `GET /login/qrcode/{session_id}`：image/png（no-store，附 `X-Qrcode-Updated-At`）；未就绪 404 `qrcode_not_ready`；
  - `GET /login/status/{session_id}`：status/message/qrcode_ready/expires_at；
  - `POST /login/{session_id}/code`：注入验证码（非 need_input → 409 `not_awaiting_code`；空码 400）；
  - `POST /login/{session_id}/cancel` 与 `DELETE /login/{session_id}`：取消会话；
  - `GET /accounts/status`：主目录扫描 + 基础判定（JSON 可解析）+ 复核说明；
  - `DELETE /accounts`：删主目录 cookie（仅存兼容目录 → 409 `fallback_readonly`；不存在 404）+ 审计 `op=account_delete`；
  - `POST /accounts/recheck`：一期落地文件级重扫（200 + `mode="file_scan"` + `checked_at` + accounts 列表，与 /accounts/status 同源扫描；真实浏览器复核留后续）；
- **成功后链路**：登录成功 → cookie 落盘 → `on_success` 回调 → `send_account_sync` 上行（`LocalApiServer` 无条件接线，含测试注入的管理器）；
- **控制台**：账号页（`AccountsView.vue`）实现登录表单（4 支持平台下拉 + 不支持平台置说明）/活跃会话面板（2s 轮询状态+二维码 blob 图/状态徽章/验证码输入/取消）/账号表格（主目录可删/兼容目录只读）；409 冲突自动复用既有 session_id 续轮询；
- **验证边界**：verify_s9 全假执行器覆盖管理器单元/HTTP 链路/账号族 27 项（含停机路径：`LocalApiServer.stop()` 先 `close_all` 取消活跃会话，消除孤儿浏览器窗口）；`default_real_executor` 的平台分发与抖音短信桥接的真实页面注入留待真机回归（需浏览器内核 + 平台网络）。

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

## 打包与发布（S8，仓库根目录）

```powershell
# 1) 构建 standalone 产物（含控制台自动构建；首次会下载 MinGW，耗时较长）
.venv\Scripts\python.exe -m sau_wrap.packaging.nuitka_build
#   → sau_wrap\packaging\out\sau.dist\sau.exe + ui\

# 2) 编译安装包（需 Inno Setup ≥6.3，x64compatible 指令所限；版本号可用 /DMyAppVersion 覆盖）
# 当前开发机实装：D:\Program Files (x86)\Inno Setup 6（6.7.3，2026-08-26 实测编译成功）
& "D:\Program Files (x86)\Inno Setup 6\ISCC.exe" `
    /DMyAppVersion=2.0.0a0 sau_wrap\packaging\installer\sau.iss
#   → sau_wrap\packaging\installer\Output\sau-{version}.exe（2.0.0a0 实测 51.4MB）

# 3) 哈希发布闭环（自动算 64 位小写 SHA-256，输出发布单）
.venv\Scripts\python.exe -m sau_wrap.packaging.hash_release
#   → installer\Output\release-manifest.json；运营按发布单上传安装包，
#     管理端 PUT /sau/upgrade-config 填三字段（版本号/download_url/sha256）
```

发布单实测样例（2.0.0a0，2026-08-26 S9 重建后）：

```json
{
  "version": "2.0.0a0",
  "installer_file": "sau-2.0.0a0.exe",
  "size_bytes": 53894041,
  "sha256": "fb57f70b9e162d416944554e57ad522a7a8a6e913c528588afa1e7c50acfa9b7",
  "download_url": "https://<部署域名>/sau/sau-2.0.0a0.exe"
}
```

构建环境版本矩阵与缺失项处理见 `packaging/BUILD_ENV.md`（§8.5）；
打包产物不入库（`packaging/.gitignore`：`out/` 与 `installer/Output/`）。

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
├── service/                   host.py 服务宿主（asyncio 接线，含 dispatcher 挂载）；ops.py 服务管理；local_api.py 5409 本地 API（S3；S6：静态托管/票据/会话/Nonce；S9：登录/账号端点族）；login_sessions.py 登录会话管理器（S9）；login_adapt.py 抖音短信二验运行时桥接（S9）
├── tests/                     mock_ws_server.py + verify_s2/s3/s4/s5/s6/s7/s9.py（本地验证，不触碰上游）
├── tray/                      瘦托盘（S5：app.py 主体，pystray + Pillow 代码生成图标；S6：票据链路）
├── console/                   Web 控制台源码（S6：Vue3 + Vite；dist/node_modules 不入库，见 console/.gitignore）
├── upgrade/                   半自动升级编排（S7：updater.py 通知校验/下载/八态状态机；orchestrator.py 可注入执行器六步编排/回滚/启动自检）
├── browser.py / doctor.py     浏览器内核安装（§8.7）与十一项体检（§14）
├── packaging/                 打包与分发（S8）：nuitka_build.py 构建脚本 / build_console.py 控制台构建 / installer/sau.iss Inno Setup 脚本 / post_install.bat 安装后编排 / hash_release.py 哈希发布闭环 / BUILD_ENV.md 版本矩阵
└── requirements.txt           包装层独立依赖清单（§8.1）
```

## 验证（mock 服务端，无需 opcgeo 后端）

```powershell
python sau_wrap\tests\verify_s2.py   # S2：WS 主循环，14/14 通过（结果写 tests\_verify_report.txt）
python sau_wrap\tests\verify_s3.py   # S3：5409 本地 API，14/14 通过（结果写 tests\_verify_report_s3.txt）
python sau_wrap\tests\verify_s4.py   # S4：任务执行核心，16/16 通过（结果写 tests\_verify_report_s4.txt）
python sau_wrap\tests\verify_s5.py   # S5：瘦托盘（模块级），29/29 通过（结果写 tests\_verify_report_s5.txt）
python sau_wrap\tests\verify_s6.py   # S6：本地 Web 控制台，31/31 通过（需先 npm run build；结果写 tests\_verify_report_s6.txt）
python sau_wrap\tests\verify_s7.py   # S7：半自动升级编排，36/36 通过（结果写 tests\_verify_report_s7.txt）
python sau_wrap\tests\verify_s9.py   # S9：登录扫码会话链路，27/27 通过（全假执行器；结果写 tests\_verify_report_s9.txt）
```

- **verify_s2**（五场景）：①注册握手 + 心跳往返 + publish_task 落库 + 优雅停止；②断线重连退避
（1011 → 实测间隔 2.02s/4.03s）；③4401 挂起零重连（6s 无新连接）+ 热重载唤醒重连；
④result_queue 离线积压（含 60 项多批）→ 补发 → 队列清空；⑤服务端主动 1000 关闭 →
退避重连不退出主循环。
- **verify_s3**（六场景）：①令牌文件生成；②401/200 鉴权 + /status 契约字段 + /config 读写（未绑定 409）+ /bind 落盘 + 占位 501（/login、/accounts/*；/ui/* 自 S6、/upgrade* 自 S7 已实现）+ 审计日志；③4401 挂起 → `POST /reload` 唤醒重连；④端口占用 → `LocalApiBindError` + 明确日志；⑤退避可被热重载打断；⑥退避期间 /config 写入即时唤醒。
- **verify_s4**（五场景，全假注入不拉起上游）：①成功链路（落库→running→mock 上传→task_result success→downloads 清理→account_sync；含成功后重推不重复执行）；②403→file_renew→file_renewed 换 URL 重试成功；③cookie 错误分类 → failed 不重试（attempts=1）；④并发信号量（3 任务峰值并发=2）；⑤重启恢复（recover_pending 扫 queued/running 重新入队执行成功）。
- **verify_s5**（七场景，模块级不启动 GUI）：①状态轮询四态（在线/离线含挂起/401/不可达，mock /status）；②状态翻转与气泡触发（进入异常提示一次且措辞与 §5.2 一字一致、同态不重复、恢复再提示、首轮不提示）；③Mutex 单实例（会话本地命名；183→None；错误注入非 183 创建失败必须报错不得误报已在运行）；④日志轮转配置（5MB×3）；⑤控制台 URL/日志目录/tooltip 构造与令牌读回；⑥图标色块生成（绿/灰 + 状态→颜色映射）；⑦日志脱敏（终审修复⑦：凭证键值掩码/Bearer 整体掩码/手机号中段掩码，覆盖句柄级 SanitizeFilter）。真实托盘交互验证见上节「S5 手动验证步骤」。
- **verify_s6**（九组场景，需先 `npm run build`）：①构建产物存在性；②静态托管（/ui/ no-cache、资源长缓存、404、路径穿越编码/明文变体均拒绝）；③dist 缺失 → 503 友好提示页；④票据全链路（签发 401 拦截/60s/核销种 Cookie（HttpOnly+SameSite=Strict）→认证访问、单次核销、过期、托盘链路：build_ticket_url/死端口回退 None/持令牌换票据）；⑤单实例顶替（旧 Cookie 401 + 日志）；⑥会话超时；⑦401 引导页（浏览器式 HTML vs API JSON；含 /ui 无尾斜杠 302 与票据核销失败引导响应）；⑧Nonce 写防护（缺失 403/一次性/重复 409/令牌豁免/审计 via=）；⑨机器码端点。控制台真实交互验证见上节「控制台访问方式」。
- **verify_s7**（十一场景，全假注入不真实停服/安装）：①通知校验与版本比较（三字段/64 位小写 hex/语义化严格大于含预发布段数字解析/强制 https/域名白名单及回退）；②后台下载（本地 http 假文件源：成功落盘校验/失败 2 次重试后成功/哈希不符删文件回 noticed 不重试）；③并发单飞锁（3 并发仅 1 次真实下载）；④状态机迁移全路径 + 原子持久化；⑤编排六步假执行器（成功序列/安装失败→自动回滚→rolled_back/回滚亦失败→failed+人工救援指引/非 ready 拒绝）；⑥启动自检三分支（§15.2：补校验 success+清备份/半替换回滚/无备份人工指引；含校验未过保持 applying、回滚失败 failed 变体）；⑦启动清理（>24h 备份与旧安装包删除、目标安装包保留）；⑧端点（/upgrade 快照、apply 未就绪 409/就绪触发编排+审计、snooze 幂等、Cookie 会话 Nonce 防护、双鉴权共存）；⑨令牌轮换回归（终审修复①：服务重启令牌轮换后校验仍通过，校验每次现读令牌文件）；⑩downloading 重启重置（终审修复⑤：重启时 downloading→noticed + .part 清理 + 可再受理）；⑪runner 移交（终审修复⑧：服务侧仅移交置 applying/移交失败 failed+救援指引/runner 进程内全流程成功且防递归）。真机验证缺口见 S7 节。
- **verify_s9**（五场景，全假执行器不真实打开平台页面）：①管理器单元语义（不支持平台拒绝/内核未装/二维码回调写入+on_success/每平台单会话冲突+终态后重建/need_input 注入送达/总超时自动回收/取消/异常兜底 failed/非 need_input 注入 False（含 Manager 级不存在会话））；②HTTP 端点链路（创建契约/重复 409/不支持 400+未知 404/二维码 404→image/png 字节一致/状态契约/注入→success→account_sync 上行/终态注入 409+空码 400/DELETE 取消/Cookie 会话 Nonce 防护/审计 + 独立实例验 503 内核引导 + 停机路径：stop() 先 close_all 活跃会话置 cancelled）；③账号族（/accounts/status 主目录扫描+is_valid 区分/删除+审计/缺参 400/不存在 404/路径穿越 400（终审修复③④）/recheck 501 占位）；④平台 CLI 透传（终审修复⑥：sau douyin --help 透传上游 + 六平台子命令注册）；⑤短信二验签名守卫（终审修复⑩：兼容通过/缺符号拦截+WARN/签名不兼容拦截）。真实平台登录回归缺口见 S9 节。
- 数据隔离于 `tests\_tmpdata*`（`SAU_DATA_ROOT` 覆盖，不触碰 `%ProgramData%\SAU`）。
