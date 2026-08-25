# 构建环境版本矩阵（重建方案 §8.5，实施计划 S8）

> 定案：实际使用的工具版本在此落档，构建产物与版本矩阵一一对应；
> 换构建机必须比对本表。产物哈希（`hash_release.py`）闭环见同目录。

## 实测版本矩阵（2026-08 构建机实测）

| 工具 | 版本 | 说明 |
| --- | --- | --- |
| 操作系统 | Windows 11 25H2（x64） | 构建机 |
| Python | 3.12.11（venv：`.venv`） | Nuitka 宿主与产物解释器 |
| Nuitka | 4.1.3 | `python -m nuitka --version` |
| C 编译器 | MinGW64（Nuitka 自动下载，`--assume-yes-for-downloads`） | 构建机无 gcc/cl；Nuitka 自管，缓存于 `%LOCALAPPDATA%\Nuitka` |
| Node.js | v24.14.0 / npm 11.9.0 | 控制台 `npm ci && npm run build` |
| Inno Setup | **6.7.3**（`D:\Program Files (x86)\Inno Setup 6`，2026-08-26 用户安装） | 最低要求 **6.3+**：`ArchitecturesAllowed=x64compatible` 为 6.3 引入的取值；`sau.iss` 实测按 6.7.3 行为修正（无 `SetupLogging` 指令、`GetSpaceOnDisk` Free/Total 为 Cardinal、脚本需 UTF-8 BOM） |
| patchright | 1.58.2 | 浏览器自动化主线（chromium revision 1208） |

## 构建纪律（§7.5 基线移植）

- `--jobs=4`：限并行 C 编译；
- `CCACHE_DISABLE=1` + `--disable-cache=all`：禁缓存读写，产物可复现；
- `--assume-yes-for-downloads`：编译器缺失由 Nuitka 自动拉取（构建内行为）；
- `--remove-output`：清理 `*.build` 中间残留；
- patchright driver 经 `--include-package-data=patchright` 单份进产物。

## 产物

- Nuitka standalone：`packaging/out/sau.dist/`（`sau.exe` + `ui/` + 运行时依赖）；
- 安装包：`packaging/installer/Output/sau-{version}.exe`（ISCC 编译，命名与升级链一致 §8.4）；
- 发布单：安装包同目录 `release-manifest.json`（`hash_release.py` 生成，64 位小写 SHA-256）。

## 缺失项处理记录

- 构建机无 gcc / cl / MinGW：Nuitka 自动下载 MinGW64（首次构建含下载耗时），不人工安装；
- 构建机无 ISCC（首检缺失，2026-08-26 用户安装 Inno Setup 6.7.3 后已闭环）；
- venv 无 `aiofiles` / `PyYAML`：运行时未引用，`INCLUDE_PACKAGES` 不列入（清单须与 site-packages 一致，否则 Nuitka FATAL）。

## 体积裁剪记录（首构建 400.9MB 实测后追加，§8.6）

| 项 | 体积 | 处置 | 依据 |
| --- | --- | --- | --- |
| playwright 浏览器二进制 | +100MB | `--disable-plugin=playwright` | 内核走 `browser install` 独立安装（§8.7）；playwright 本体（driver）保留，上游 baijiahao 仍引 `playwright.async_api` |
| cv2 | 98.6MB | `--nofollow-import-to=cv2` | 仅 `utils/login_qrcode.py` 登录二维码链路引用；登录会话族不在包装层承载（/login 501 占位，§6.5） |
| numpy | ~26MB | `--nofollow-import-to=numpy` | 仅作为 cv2 伴生依赖；全仓 grep 无直接引用 |
| stream_gears | 32.5MB | `--nofollow-import-to=stream_gears` | biliup 内部推流依赖；biliup 经独立二进制调用（上游 `run_biliup_command` 纯 subprocess，首次使用按需下载） |
| biliup 包 | — | 移出 `INCLUDE_PACKAGES` | 同上；包进产物会带入无效依赖链 |

另：Nuitka 4.1.3 无 `--output-dir-name`，产物目录默认随入口文件名 `entry.dist`，构建后由脚本改名 `sau.dist`（sau.iss BuildDir 定案路径）。
