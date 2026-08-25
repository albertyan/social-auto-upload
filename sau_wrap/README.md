# sau_wrap —— SAU 客户端包装层（任务 #12 第一步原型）

按《SAU客户端重建方案-单EXE与包装层设计》实施计划 **S1 + S4 前置最小原型** 落地：
单一入口子命令分发 + pywin32 服务宿主骨架。**上游源码零修改**（只新增本目录）。

## 本步已实现

| 能力 | 状态 |
| --- | --- |
| 子命令分发框架（Click） | ✅ `agent / service / tray / browser / doctor / machine-code / bind` |
| `service install` | ✅ 注册 + 延迟自启（DELAYED_AUTO_START）+ 失败梯度重启（30/60/120s，24h 重置） |
| `service remove/start/stop/status` | ✅（start 轮询窗口 60s，stop 等待 30s） |
| `service upgrade` | ⬜ 占位（S7） |
| `agent` | ✅ 空跑骨架：写日志、保持运行、响应停止（不连 WS） |
| `tray / browser / doctor / machine-code / bind` | ⬜ 占位 |
| 版本号 | ✅ `version.py` 的 `APP_VERSION`（可被环境变量 `SAU_VERSION` 覆盖），`--version` 显示 |
| 日志 | ✅ `%ProgramData%\SAU\logs\service.log`（10MB × 5 轮转） |

## 运行方式（开发环境，仓库根目录）

```powershell
# 依赖（当前环境已具备；新环境执行）
pip install -r sau_wrap\requirements.txt

# 帮助与版本
python -m sau_wrap --help
python -m sau_wrap --version

# 前台调试运行服务骨架（Ctrl+C 或窗口关闭退出）
python -m sau_wrap agent run-fg

# 服务生命周期（需管理员权限）
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

## 目录结构（§3.3，本步仅骨架）

```
sau_wrap/
├── __main__.py / entry.py     入口（python -m sau_wrap / sau.exe）
├── version.py                 版本单一事实源（SAU_VERSION 可覆盖）
├── paths.py / logutil.py      运行时数据布局（§3.6）与日志（§14.1）
├── agent/                     core.py 空跑骨架；ws_client/dispatcher/accounts 占位（S2）
├── service/                   host.py 服务宿主；ops.py 服务管理（S4）；server 等后续（S3）
├── tray/                      占位（S5）
├── console/                   占位（S6）
├── upgrade/                   占位（S7）
├── packaging/                 占位（S8）
└── requirements.txt           包装层独立依赖清单（§8.1）
```
