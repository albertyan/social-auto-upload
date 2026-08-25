# sau_wrap —— SAU 客户端包装层（S1/S4 已落，S2：Agent WS 核心）

按《SAU客户端重建方案-单EXE与包装层设计》实施计划推进：
单一入口子命令分发 + pywin32 服务宿主 + **Agent WS 主循环（S2）**。
**上游源码零修改**（只新增本目录）。

## 实现状态

| 能力 | 状态 |
| --- | --- |
| 子命令分发框架（Click） | ✅ `agent / service / tray / browser / doctor / machine-code / bind` |
| `service install` | ✅ 注册 + 延迟自启（DELAYED_AUTO_START）+ 失败梯度重启（30/60/120s，24h 重置）；重复安装（1073）友好提示 |
| `service remove/start/stop/status` | ✅（start 轮询窗口 60s，stop 等待 30s） |
| `service upgrade` | ⬜ 占位（S7） |
| `agent` | ✅ WS 主循环（S2）：注册/心跳/重连退避/凭证类关闭码挂起/任务落库/结果补发；服务异常以失败态退出触发 SCM 重启 |
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
- **停止**：响应服务停止事件，关闭连接后退出主循环。

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
│   ├── ws_client.py           WS 主循环（S2 核心）
│   └── dispatcher.py / accounts.py   占位（任务调度/账号，后续步骤）
├── service/                   host.py 服务宿主（asyncio 接线）；ops.py 服务管理；server 等后续（S3）
├── tests/                     mock_ws_server.py + verify_s2.py（本地验证，不触碰上游）
├── tray/                      占位（S5）
├── console/                   占位（S6）
├── upgrade/                   占位（S7）
├── packaging/                 占位（S8）
└── requirements.txt           包装层独立依赖清单（§8.1）
```

## 本步验证（mock 服务端，无需 opcgeo 后端）

```powershell
python sau_wrap\tests\verify_s2.py
```

四场景：①注册握手 + 心跳往返 + publish_task 落库 + 优雅停止；②断线重连退避
（1011 → 实测间隔 2.02s/4.03s）；③4401 挂起零重连（6s 无新连接）+ 热重载唤醒重连；
④result_queue 离线积压 → 连接后补发 → 队列清空。结果写
`tests\_verify_report.txt`（最近一次 12/12 通过）。数据隔离于
`tests\_tmpdata`（`SAU_DATA_ROOT` 覆盖，不触碰 `%ProgramData%\SAU`）。
