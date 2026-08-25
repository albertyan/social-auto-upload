# sau_wrap —— SAU 客户端包装层（S1 已落，S2：Agent WS 核心，S3：5409 本地 API，S4：任务执行核心）

按《SAU客户端重建方案-单EXE与包装层设计》实施计划推进：
单一入口子命令分发 + pywin32 服务宿主 + **Agent WS 主循环（S2）** +
**5409 本地 API（S3）** + **任务执行核心（S4：dispatcher）**。
**上游源码零修改**（只新增本目录）。

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
| `/ui/*`、登录/账号/升级端点 | ⬜ 占位 501（S5/S6/S7） |
| `machine-code` | ✅ 真实机器码（SHA-256(MachineGuid+卷序列号+CPU ID) 前 32 位，§5.7） |
| `bind` | ✅ 写 `config.json` + `credential.bin`（DPAPI LOCAL_MACHINE） |
| `tray / browser / doctor` | ⬜ 占位 |
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
  - `/ui/*`、`/ui-ticket`、`/login`、`/accounts/*`、`/upgrade*`：占位 501 + 说明（控制台/登录/升级留后续步骤）；
- **审计**：绑定/配置写入/热重载在 service.log 记一行 `[AUDIT] op=… source=127.0.0.1 result=… detail=…`。

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
├── service/                   host.py 服务宿主（asyncio 接线，含 dispatcher 挂载）；ops.py 服务管理；local_api.py 5409 本地 API（S3）
├── tests/                     mock_ws_server.py + verify_s2/s3/s4.py（本地验证，不触碰上游）
├── tray/                      占位（S5）
├── console/                   占位（S6）
├── upgrade/                   占位（S7）
├── packaging/                 占位（S8）
└── requirements.txt           包装层独立依赖清单（§8.1）
```

## 验证（mock 服务端，无需 opcgeo 后端）

```powershell
python sau_wrap\tests\verify_s2.py   # S2：WS 主循环，14/14 通过（结果写 tests\_verify_report.txt）
python sau_wrap\tests\verify_s3.py   # S3：5409 本地 API，14/14 通过（结果写 tests\_verify_report_s3.txt）
python sau_wrap\tests\verify_s4.py   # S4：任务执行核心，16/16 通过（结果写 tests\_verify_report_s4.txt）
```

- **verify_s2**（五场景）：①注册握手 + 心跳往返 + publish_task 落库 + 优雅停止；②断线重连退避
（1011 → 实测间隔 2.02s/4.03s）；③4401 挂起零重连（6s 无新连接）+ 热重载唤醒重连；
④result_queue 离线积压（含 60 项多批）→ 补发 → 队列清空；⑤服务端主动 1000 关闭 →
退避重连不退出主循环。
- **verify_s3**（六场景）：①令牌文件生成；②401/200 鉴权 + /status 契约字段 + /config 读写（未绑定 409）+ /bind 落盘 + 占位 501 + 审计日志；③4401 挂起 → `POST /reload` 唤醒重连；④端口占用 → `LocalApiBindError` + 明确日志；⑤退避可被热重载打断；⑥退避期间 /config 写入即时唤醒。
- **verify_s4**（五场景，全假注入不拉起上游）：①成功链路（落库→running→mock 上传→task_result success→downloads 清理→account_sync；含成功后重推不重复执行）；②403→file_renew→file_renewed 换 URL 重试成功；③cookie 错误分类 → failed 不重试（attempts=1）；④并发信号量（3 任务峰值并发=2）；⑤重启恢复（recover_pending 扫 queued/running 重新入队执行成功）。
- 数据隔离于 `tests\_tmpdata*`（`SAU_DATA_ROOT` 覆盖，不触碰 `%ProgramData%\SAU`）。
