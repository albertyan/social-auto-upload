# 构建控制台前端（dist 已存在会自动跳过）
.venv\Scripts\python.exe -m sau_wrap.packaging.build_console           # 或加 --force 强制重建

# 开发基线版本（读 version.py，当前 2.0.0a0）
.venv\Scripts\python.exe -m sau_wrap.packaging.nuitka_build

# 正式版本：环境变量注入（优先级最高）
$env:SAU_VERSION='2.0.0'
.venv\Scripts\python.exe -m sau_wrap.packaging.nuitka_build

# Inno Setup 编译安装包
& "D:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=2.0.0 sau_wrap\packaging\installer\sau.iss

# 生成发布单（SHA-256 哈希闭环）
.venv\Scripts\python.exe -m sau_wrap.packaging.hash_release

# 发布上线（运营侧）
1. 把 sau-{version}.exe 上传到 HTTPS 白名单域名存储（客户端强制 https + 域名白名单）；
2. 管理端调 PUT /sau/upgrade-config，填 version / download_url / file_hash（即发布单哈希），并置 enabled=1；
3. 保存即向在线 Agent 广播 upgrade_notice，客户端自动走下载→确认→静默安装→回滚保护链路。

# 一条龙最简序列
```cmd
cd d:\dev\workspace\social-auto-upload
$env:SAU_VERSION='2.0.0'
.venv\Scripts\python.exe -m sau_wrap.packaging.nuitka_build
& "D:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=2.0.0 sau_wrap\packaging\installer\sau.iss
.venv\Scripts\python.exe -m sau_wrap.packaging.hash_release
$env:SAU_VERSION=''
```

# release-manifest.json 是安装包发布的"出厂合格证"——把安装包交给运营/服务端配置升级推送时的唯一数据源。用法如下：

```json
{
  "version": "2.0.0a0",
  "installer_file": "sau-2.0.0a0.exe",
  "size_bytes": 87604033,
  "sha256": "8befadc4dbe169c27ea9abb3faddc68f6c764745e81f86315662d0d2a94fb0a5",
  "build_time": "...",
  "build_host": "...",
  "download_url": "https://<部署域名>/sau/sau-2.0.0a0.exe"   // 占位，需运营回填真实地址
}
```

# 使用流程（3 步）
## 第 1 步：上传安装包
把 installer\Output\sau-{version}.exe 上传到 HTTPS 域名的文件存储（如 OSS/静态资源服务器），记下真实下载地址。注意两点：
必须 https://（客户端强制）；
域名要在客户端白名单内（默认取绑定地址 server_url 的 host 及其子域，也可配置项覆盖）。
## 第 2 步：配置服务端升级
在 opcgeo 管理端（或直接调接口）更新升级配置 PUT /opcgeo/sau/upgrade-config，把发布单三个字段对应填入：
|发布单字段	|服务端字段（biz_sau_upgrade_config）|说明|
|------|----------|------|
|version|version|目标版本号|
|第 1 步的真实下载地址|download_url|替换发布单里的占位地址|
|sha256|file_hash|64 位小写，服务端入库时校验格式|
然后置 enabled = 1。

## 第 3 步：广播生效
保存（启用状态）即自动向所有在线 Agent 推送 upgrade_notice；离线的 Agent 下次建连时也会补推。客户端收到后自动：校验版本严格大于当前版 → 后台下载并边下边算 SHA-256 → **与发布单哈希不一致直接拒绝**（删文件回退）→ 控制台显示"可升级"等用户确认 → 静默安装 + 自动回滚保护。