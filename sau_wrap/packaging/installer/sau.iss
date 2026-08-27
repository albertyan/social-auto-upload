; ============================================================
; sau.iss —— SAU 安装包（Inno Setup 6，实施计划 S8；重建方案 §7.3/§13/§17）
;
; 编译（/D 可覆盖版本与构建目录）：
;   ISCC /DMyAppVersion=2.0.0a0 sau_wrap\packaging\installer\sau.iss
;   → 产物 installer\Output\sau-{version}.exe（与升级链命名一致，§8.4）
;
; 覆盖场景（§7.3 编者注）：干净机器首次安装 + 新布局自身版本间覆盖升级。
; 卸载六步见 §13；安装失败矩阵见 §17；浏览器内核下载在安装向导内后台执行，
; 交互模式借安装页 ProgressGauge 轮询进度文件实时展示（任务 #10，替代任务 #3
; 已证实失效的可见控制台窗口方案——sau.exe 为 GUI 子系统无控制台）；
; 静默模式维持隐藏阻塞等待。
; ============================================================

#define MyAppName "SAU"
#ifndef MyAppVersion
  #define MyAppVersion "2.0.0a0"
#endif
#ifndef BuildDir
  #define BuildDir "..\out\sau.dist"
#endif
#define MyServiceName "SAUAgentService"
#define MyDataDir "{commonappdata}\SAU"

[Setup]
; AppId 固定不变：支撑新布局版本间覆盖升级（§7.3/§8.4）
AppId={{B7E4F2A1-9C3D-4E58-A6F0-2D8C1B9E7A53}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=SAU
DefaultDirName={autopf}\SAU
DefaultGroupName={#MyAppName}
PrivilegesRequired=admin
OutputDir=Output
OutputBaseFilename=sau-{#MyAppVersion}
; 体积优化⑤（§8.6）：LZMA2 ultra + SolidCompression
Compression=lzma2/ultra64
SolidCompression=yes
; Inno Setup 6 安装日志默认启用（安装时 /LOG=<path> 生效，§14.1 落 {app}）；
; 6.x 无 SetupLogging 指令，不声明。
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\sau.exe
WizardStyle=modern

[Languages]
; 任务 #3 中文化：仅保留简体中文一条，避免启动时弹语言选择对话框；
; 静默安装可用 /LANG=chinesesimplified 显式指定。
; 原英文条目注释保留备查：
; Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "Languages\ChineseSimplified.isl"

[Files]
; §7.3 首装步骤 1：整目录递归释放 {app}（sau.exe + ui/ + 运行时依赖）
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; post-install 编排脚本（§17 失败不静默）
Source: "..\post_install.bat"; DestDir: "{app}"; Flags: ignoreversion

[Tasks]
; 桌面快捷方式：安装向导"附加任务"页可勾选，默认创建。
Name: "desktopicon"; Description: "创建桌面快捷方式(&D)"; GroupDescription: "附加快捷方式:"

[Icons]
; 桌面快捷方式（受 [Tasks] desktopicon 控制）
Name: "{autodesktop}\SAU"; Filename: "{app}\sau.exe"; Parameters: "tray"; \
  WorkingDir: "{app}"; IconFilename: "{app}\sau.exe"; \
  Comment: "SAU Agent - 社交媒体自动化上传"; Tasks: desktopicon
; 开始菜单快捷方式（DefaultGroupName = {#MyAppName} = SAU）
Name: "{group}\SAU"; Filename: "{app}\sau.exe"; Parameters: "tray"; \
  WorkingDir: "{app}"; IconFilename: "{app}\sau.exe"; \
  Comment: "SAU Agent - 社交媒体自动化上传"

[Run]
; 任务 #26（托盘缺失修复）：sau.iss 原本无 [Run] 段，装完从不拉起托盘；
; post-install 异常中断时 HKCU 自启项也未写入 → 重启后托盘同样缺席。
; postinstall = 完成页勾选项（默认勾选）；nowait 不阻塞向导收尾；
; 静默安装跳过（托盘依赖交互会话）；托盘自身单实例 Mutex 兜底重复启动。
Filename: "{app}\sau.exe"; Parameters: "tray"; \
  Description: "启动 SAU 托盘（后台常驻，任务 #26）"; \
  Flags: postinstall nowait skipifsilent skipifdoesntexist

[Code]
const
  // ---------------- 任务 #10：安装页浏览器内核下载进度条常量 ----------------
  BrowserPollMs = 500;        // 进度文件轮询间隔（毫秒；Sleep 同时泵消息，向导不假死）
  BrowserStallSeqs = 180;     // seq 心跳停滞阈值（180 次轮询 ≈ 90 秒）→ 判下载进程崩溃
  BrowserMaxPolls = 2580;     // 轮询硬上限（≈21.5 分钟，覆盖 Python 侧 20 分钟 deadline）

var
  // 任务 #3：是否同时删除本地数据（%ProgramData%\SAU），卸载前弹框询问，默认保留；
  // 替代原卸载进度窗勾选框（一闪而过来不及勾）。凭证文件始终删除，不受此开关控制。
  DeleteLocalData: Boolean;

// ---------------- §17 阶段 1：前置检查（失败中止，系统无变更）----------------

function InitializeSetup(): Boolean;
var
  Version: TWindowsVersion;
  // Inno 6.7.3 的 GetSpaceOnDisk Free/Total 为 Cardinal（探针实测，Int64 报 Type mismatch）；
  // 2048MB 阈值在 Cardinal 范围内，足够覆盖 §17 阶段 1 检查。
  FreeMB, TotalMB: Cardinal;
begin
  Result := True;
  GetWindowsVersionEx(Version);
  if (Version.Major < 10) then
  begin
    MsgBox('SAU 需要 Windows 10 及以上系统。', mbError, MB_OK);
    Result := False;
    Exit;
  end;
  // 磁盘不足提示用纯 ASCII，规避 ANSI/UTF-8 码页差异（§17 阶段 1）
  if not GetSpaceOnDisk(ExtractFileDrive(ExpandConstant('{commonappdata}')), True, FreeMB, TotalMB) then
    FreeMB := 999999;
  if (FreeMB < 2048) then
  begin
    MsgBox(Format('Disk space insufficient: need >= 2048 MB, free %d MB.', [FreeMB]), mbError, MB_OK);
    Result := False;
    Exit;
  end;
end;

// ---------------- 覆盖升级：停服等待句柄释放（§7.3 / §4.2）----------------

procedure ExecHide(const Filename, Params: String);
var
  ResultCode: Integer;
begin
  Exec(Filename, Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

// 提取 CSV 行首引号包裹的第一个字段（schtasks /FO CSV 的 TaskName 列）。
function ExtractFirstCsvField(const Line: String): String;
var
  i, StartPos: Integer;
begin
  Result := '';
  StartPos := Pos('"', Line);
  if StartPos = 0 then
    Exit;
  for i := StartPos + 1 to Length(Line) do
  begin
    if Line[i] = '"' then
    begin
      Result := Copy(Line, StartPos + 1, i - StartPos - 1);
      Exit;
    end;
  end;
end;

// 卸载清理（评审修复 #4）：删除残留的 SAU\HeadedLogin-* 计划任务。
// 服务侧残留由启动序列 cleanup_stale 收敛，但卸载后服务不复存在，此处兜底清理。
// schtasks /Query /FO CSV /NH 输出到临时文件 → 逐行匹配前缀 → /Delete /F；
// 失败静默容忍（卸载不应因计划任务清理失败而报错）。
procedure DeleteHeadedLoginTasks();
var
  CsvFile: String;
  Lines: TArrayOfString;
  i: Integer;
  Line, TaskName: String;
  ResultCode: Integer;
begin
  CsvFile := ExpandConstant('{tmp}\sau_headed_tasks.csv');
  DeleteFile(CsvFile);
  if not Exec('cmd.exe', '/c schtasks.exe /Query /FO CSV /NH > "' + CsvFile + '" 2>nul',
              '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Exit;
  if (ResultCode <> 0) or not FileExists(CsvFile) then
    Exit;
  if not LoadStringsFromFile(CsvFile, Lines) then
  begin
    DeleteFile(CsvFile);
    Exit;
  end;
  for i := 0 to GetArrayLength(Lines) - 1 do
  begin
    Line := Lines[i];
    if Pos('SAU\HeadedLogin-', Line) > 0 then
    begin
      TaskName := ExtractFirstCsvField(Line);
      if TaskName <> '' then
        Exec('schtasks.exe', '/Delete /TN "' + TaskName + '" /F',
             '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
  end;
  DeleteFile(CsvFile);
end;

// 静默模式判定（任务 #3；评审问题 2 修复：追加 /SUPPRESSMSGBOXES）：本机 Inno 6.7.3 无
// WizardSilentMode / IsSilentInstall / CmdLineParamExists（探针实测均报 Unknown identifier），
// 改用 GetCmdTail 自查命令行；安装与卸载进程同一套参数，大小写不敏感匹配。
// 口径为「隐藏交互/静默判定」：/SILENT、/VERYSILENT、/SUPPRESSMSGBOXES 三参数任一即命中——
// 仅传 /SUPPRESSMSGBOXES（交互式但不想被打扰的自动化组合）时同样隐藏内核下载窗口，
// 避免弹出最长 20 分钟的可见控制台。
// 副作用评估（卸载弹框询问复用本函数）：/SUPPRESSMSGBOXES 时卸载询问本就走
// SuppressibleMsgBox 自动取默认值 IDNO（保留数据），SauSilentMode 命中后跳过询问，
// 结果相同（保留数据），语义一致，无副作用。
function SauSilentMode(): Boolean;
var
  Tail: String;
begin
  Tail := LowerCase(GetCmdTail());
  Result := (Pos('/silent', Tail) > 0) or (Pos('/verysilent', Tail) > 0)
    or (Pos('/suppressmsgboxes', Tail) > 0);
end;

function FileHandleFree(const Path: String): Boolean;
var
  ProbePath: String;
  ResultCode: Integer;
begin
  // Inno Pascal Script 无 FileOpen/fmShareExclusive；改用 rename 探测：
  // rename 对已被进程句柄占用的 PE 文件会失败（ERROR_SHARING_VIOLATION），
  // 成功则立即改回（原目录非卷根，无跨卷问题）
  Result := False;
  if not FileExists(Path) then
  begin
    Result := True;
    Exit;
  end;
  ProbePath := Path + '.sau-lock-probe';
  if Exec('cmd.exe', '/c rename "' + Path + '" "sau.exe.sau-lock-probe"',
          ExtractFilePath(Path), SW_HIDE, ewWaitUntilTerminated, ResultCode)
     and (ResultCode = 0) and FileExists(ProbePath) then
  begin
    Exec('cmd.exe', '/c rename "' + ProbePath + '" "sau.exe"',
         ExtractFilePath(Path), SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Result := True;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  i: Integer;
  ImagePath: String;
  SauExe: String;
begin
  Result := '';
  NeedsRestart := False;
  SauExe := ExpandConstant('{app}\sau.exe');
  if not FileExists(SauExe) then
    Exit;  // 首次安装，无存量（§7.3）

  // 防御性：残留旧服务名指向非 sau.exe 的 ImagePath → 先 remove（§7.3）
  if RegQueryStringValue(HKEY_LOCAL_MACHINE,
    'SYSTEM\CurrentControlSet\Services\{#MyServiceName}', 'ImagePath', ImagePath) then
  begin
    if Pos('sau.exe', LowerCase(ImagePath)) = 0 then
      ExecHide('sc.exe', 'delete {#MyServiceName}');
  end;

  // 先礼后兵（任务 #3）：命名事件通知托盘优雅退出，避免 taskkill 强杀留下幽灵托盘图标；
  // 旧版本无该命令时事件不存在、秒返回退出码 0，行为等同现状，随后 taskkill 兜底。
  // 显式传 --timeout 15（修复 2）：托盘侧探测已前移到轮询循环最前（最坏延迟 ≤1 个
  // POLL_INTERVAL≈5s），15s 窗口留足余量，避免贴边超时退回 taskkill（幽灵图标回归）。
  ExecHide(SauExe, 'tray-exit --timeout 15');
  // 杀托盘/残留进程（taskkill 找不到进程时非零退出，无害）
  ExecHide('taskkill.exe', '/f /im sau.exe');
  // 停服（覆盖升级必须，§7.3）
  ExecHide('sc.exe', 'stop {#MyServiceName}');
  // 等待进程退出与文件句柄释放（最多 30s，§4.2）
  for i := 1 to 30 do
  begin
    if FileHandleFree(SauExe) then
      Break;
    Sleep(1000);
  end;
  if not FileHandleFree(SauExe) then
    Result := 'SAU 服务进程仍在占用文件，请手动结束 sau.exe 后重试。';
end;

// ---------------- 首装步骤 2~5：数据目录/权限/服务/自启（post_install.bat）---
//
// 任务 #26（结果码处理重构）：
// - bat 退出码矩阵闭环：0 成功 / 11 注册失败（阶段 3）/ 12 启动失败（阶段 4）/
//   其余一律「未知」并显示十进制原始码（旧版 13~20 区间连弹窗都不触发，
//   且 IntToStr 已是十进制——用户侧 "$17" 十六进制误导口径彻底消除）；
// - Exec 失败（cmd 未能启动）映射为 99 报未知，不再伪装 -1；
// - 任何非 0 都弹窗（旧条件漏掉 13~20，未知码静默放行）。
//
// 任务 #3 → 任务 #10：内核下载从 bat 隐藏窗口移至安装向导，可见控制台窗口方案
// 已证实失效（sau.exe 为 GUI 子系统、无控制台，任务 #8 锤实），改为：
// - 交互模式：ewNoWait 后台拉起 + 轮询进度文件（{commonappdata}\SAU\download_progress.ini），
//   借用 ssPostInstall 期间空闲的 WizardForm.ProgressGauge 实时展示；
//   进度机制 = done 终态 + seq 心跳（Exec 不返回进程句柄，只能以进度文件判活：
//   Python 侧每次写盘 seq +1，停滞 ≥180 次轮询 ≈90 秒判崩溃；超时后不 taskkill
//   sau.exe——会误杀托盘/服务，Python 侧 20 分钟 deadline 自收敛）；
//   进度文件值域必须纯 ASCII（Inno 对无 BOM 文件按 GBK 解析）；
//   下载期间禁用 Cancel 按钮（防中途取消留孤儿下载进程、跳过 [Run] 托盘拉起），
//   try/finally 恢复；
// - 静默模式：维持现状语义——SW_HIDE + ewWaitUntilTerminated 阻塞等待；
// - 下载失败不阻断安装收尾，但弹窗提示手动补救（失败不静默，与 §17 口径一致）；
// - ISPP 陷阱规避：新增代码避免行首 '[' 数组字面量（会被误判段标签）。

// 进度文件 stage 枚举 → 中文文案（任务 #10；枚举值为 Python 侧写入的纯 ASCII）
function MapStageText(const Stage: String): String;
begin
  if Stage = 'downloading' then
    Result := '下载中 '
  else if Stage = 'extracting' then
    Result := '解压中 '
  else if Stage = 'retrying' then
    Result := '网络换源重试中 '
  else if Stage = 'starting' then
    Result := '准备中 '
  else if Stage = 'done' then
    Result := '完成 '
  else if Stage = 'failed' then
    Result := '失败 '
  else if Stage = 'timeout' then
    Result := '超时 '
  else
    Result := '进行中 ';
end;

// 交互模式内核下载（任务 #10）：ewNoWait 后台拉起 + 500ms 轮询进度文件，
// 借用 WizardForm.ProgressGauge 展示总百分比。返回 0=成功，非 0=失败
// （含进程拉起失败 -1 / 心跳停滞或超时 -1，调用方统一弹不阻断窗）。
function BrowserInstallInteractive(): Integer;
var
  ProgressFile, Stage: String;
  RC, Pct, Seq, LastSeq, StallCount, Polls: Integer;
  CancelWasEnabled: Boolean;
begin
  Result := -1;
  ProgressFile := ExpandConstant('{commonappdata}\SAU\download_progress.ini');
  DeleteFile(ProgressFile);  // 清旧残留：防上次遗留的 done=1 被误判为已完成
  if not Exec(ExpandConstant('{app}\sau.exe'),
              'browser install --progress-file "' + ProgressFile + '"',
              '', SW_HIDE, ewNoWait, RC) then
    Exit;  // Exec 返回 False：进程未能启动，立即按失败处理（调用方弹窗）
  WizardForm.ProgressGauge.Max := 100;
  WizardForm.ProgressGauge.Position := 0;
  // 记录并禁用 Cancel：中途取消会留孤儿下载进程且跳过 [Run] 托盘拉起；
  // try/finally 保证任何路径都恢复（含 done/停滞/超时 Break）
  CancelWasEnabled := WizardForm.CancelButton.Enabled;
  WizardForm.CancelButton.Enabled := False;
  try
    LastSeq := -1;
    StallCount := 0;
    Polls := 0;
    while True do
    begin
      Sleep(BrowserPollMs);  // Sleep 泵消息，向导 UI 不假死（任务 #8 研究结论）
      Polls := Polls + 1;
      // GetIniString 读进度文件（值域纯 ASCII；StrToIntDef 兜底，6.7.3 探针实测可用）
      Stage := GetIniString('progress', 'stage', '', ProgressFile);
      Pct := StrToIntDef(GetIniString('progress', 'percent', '0', ProgressFile), 0);
      Seq := StrToIntDef(GetIniString('progress', 'seq', '0', ProgressFile), 0);
      if (Pct >= 0) and (Pct <= 100) then
        WizardForm.ProgressGauge.Position := Pct;
      WizardForm.StatusLabel.Caption := '正在下载浏览器内核（约 295MB）：'
        + MapStageText(Stage) + IntToStr(Pct) + '%';
      if StrToIntDef(GetIniString('result', 'done', '0', ProgressFile), 0) = 1 then
      begin
        // 进程已正常结束（含失败）：按 [result] exit_code 判成败
        Result := StrToIntDef(GetIniString('result', 'exit_code', '1', ProgressFile), 1);
        Break;
      end;
      if Seq = LastSeq then
        StallCount := StallCount + 1  // seq 心跳停滞累计（下载进程可能已崩溃）
      else
      begin
        LastSeq := Seq;
        StallCount := 0;
      end;
      if (StallCount >= BrowserStallSeqs) or (Polls >= BrowserMaxPolls) then
        Break;  // 停滞判死 / 轮询硬上限；不 taskkill（会误杀托盘/服务），Python 侧自收敛
    end;
  finally
    WizardForm.CancelButton.Enabled := CancelWasEnabled;  // 恢复 Cancel 按钮
    WizardForm.StatusLabel.Caption := '正在完成安装…';  // 文案还原（收尾阶段照常）
    WizardForm.ProgressGauge.Position := WizardForm.ProgressGauge.Max;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ExitCode: Integer;
  BrowserRC: Integer;
  Msg: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  // §7.3 步骤 2~6 由 post_install.bat 编排（含 §17 阶段 3/4/6 的明确报错）
  if not Exec('cmd.exe', '/c ""' + ExpandConstant('{app}\post_install.bat')
              + '" > "' + ExpandConstant('{app}\post_install.log') + '" 2>&1"',
              '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then
    ExitCode := 99;  // cmd.exe 未能启动：报未知码，不得静默放行（任务 #26）
  if ExitCode <> 0 then
  begin
    case ExitCode of
      11: Msg := '服务注册失败（阶段 3，退出码 11）：请查看 ' + ExpandConstant('{app}\post_install.log') + '，' + #13#10
                 + '或手动执行 "sau.exe service install"，再运行 "sau.exe doctor" 排障。';
      12: Msg := '服务启动失败（阶段 4，退出码 12，已重试 2 次）：请运行 "sau.exe doctor" 排障，' + #13#10
                 + '详情见 ' + ExpandConstant('{app}\post_install.log') + ' 与 '
                 + ExpandConstant('{commonappdata}\SAU\logs\service.log') + '。';
      else Msg := 'post-install 异常（未知退出码 ' + IntToStr(ExitCode) + '，十进制）：' + #13#10
                 + '详见 ' + ExpandConstant('{app}\post_install.log') + '；' + #13#10
                 + '可手动执行 "sau.exe service install" 与 "sau.exe doctor" 排障。';
      // 注：不用多行 Format([...])——数组常量 '[' 位于行首会被 ISPP 误判为段标签；
      // IntToStr 输出十进制，阶段归属与退出码矩阵严格一一对应（任务 #26）
    end;
    SuppressibleMsgBox(Msg, mbError, MB_OK, IDOK);
    Exit;  // bat 阶段失败时不再继续内核下载，避免叠加报错干扰排障（任务 #3）
  end;
  // 任务 #10：post_install 成功后追加内核下载步（约 295MB，最长 20 分钟）；
  // sau.exe 为 GUI 子系统无控制台（任务 #3 可见窗口方案已证伪），改后台执行 +
  // 进度文件轮询：交互模式借安装页 ProgressGauge，静默维持隐藏阻塞等待。
  WizardForm.StatusLabel.Caption := '正在下载浏览器内核（约 295MB），请勿关闭窗口…';
  if FileExists(ExpandConstant('{app}\sau.exe')) then
  begin
    if SauSilentMode() then
    begin
      // 静默（/SILENT /VERYSILENT /SUPPRESSMSGBOXES 任一）：维持现状语义——隐藏阻塞等待，
      // Exec 返回 False（进程未能启动）时 BrowserRC 未被赋值，显式置 -1 统一走失败分支（任务 #3）
      if not Exec(ExpandConstant('{app}\sau.exe'), 'browser install', '', SW_HIDE,
                  ewWaitUntilTerminated, BrowserRC) then
        BrowserRC := -1;
    end
    else
      BrowserRC := BrowserInstallInteractive();  // 交互：进度条轮询模式（任务 #10）
    if BrowserRC <> 0 then
      SuppressibleMsgBox('浏览器内核下载未完成，不影响本次安装。可稍后手动执行 "sau.exe browser install"，' + #13#10
        + '或使用离线包 "sau.exe browser install --from-file <zip>"。', mbError, MB_OK, IDOK);
  end;
end;

// ---------------- 卸载（§13 六步）----------------
// 卸载数据处置策略（任务 #3）：凭证（DPAPI 加密的 Agent Token）始终删除，
// 防止卸载后残留可被重装复用；其余数据卸载前弹框询问，默认保留
// （原卸载进度窗勾选框一闪而过来不及勾，改为卸载前询问，时间充裕）。
// 数据根取 {#MyDataDir}（= {commonappdata}\SAU，脚本头部定义）；
// SAU_DATA_ROOT 环境变量为测试隔离约定，安装器不感知（边界：安装器只认生产路径）；
// 整删数据目录与先删凭证无冲突（凭证在数据目录内，先删凭证只是冗余安全确保）。

// 删除单个凭证文件：DeleteFile 对不存在文件静默失败；删除后逐个校验，
// 仍存在则等待重试最多 3 次；仍失败则报错——敏感凭证不得静默跳过（任务 #3）。
// 修复 1：失败提示改用**不可抑制**的 MsgBox 并先 Log 留痕。原 SuppressibleMsgBox
// 在 /VERYSILENT + /SUPPRESSMSGBOXES 下被 Inno 自动抑制，凭证残留却无任何提示，
// 违背「安全事项不得静默跳过」口径；注意 MsgBox 在 /VERYSILENT 下仍会弹出——
// 这是刻意的安全例外（敏感凭证残留必须让用户知晓，不得静默跳过）。
procedure DeleteCredentialFile(const Path: String);
var
  i: Integer;
begin
  DeleteFile(Path);
  for i := 1 to 3 do
  begin
    if not FileExists(Path) then
      Exit;
    Sleep(500);  // 可能被占用（服务/托盘已在 usUninstall 停止），稍后重试（任务 #3）
    DeleteFile(Path);
  end;
  if FileExists(Path) then
  begin
    Log('SAU credential delete failed: ' + Path);  // 先留痕（修复 1：静默卸载下也有据可查）
    // 不可抑制弹窗：安全例外，/VERYSILENT 下亦必须提示手动删除（修复 1）
    MsgBox('敏感凭证文件删除失败（可能被占用），请手动删除：' + Path, mbError, MB_OK);
  end;
end;

// 卸载前弹框询问是否删除本地数据；静默卸载（/SILENT /VERYSILENT /SUPPRESSMSGBOXES，
// 见 SauSilentMode 三参数口径）跳过弹框直接保留。
// DefaultResult 为 IDNO：/SUPPRESSMSGBOXES 场景原本走 SuppressibleMsgBox 自动选「否」→
// 保留数据，与 SauSilentMode 命中后跳过询问的结果相同（评审问题 2，语义一致无副作用）。
function InitializeUninstall(): Boolean;
begin
  Result := True;
  DeleteLocalData := False;
  if SauSilentMode() then
    Exit;
  DeleteLocalData := SuppressibleMsgBox(
      '是否同时删除本地数据（%ProgramData%\SAU 的 cookies、任务库等）？' + #13#10
      + '选择"否"将保留，重装后可恢复登录态。' + #13#10
      + '注意：本机凭证文件将始终删除。',
      mbConfirmation, MB_YESNO, IDNO) = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  SauExe: String;
begin
  case CurUninstallStep of
    usUninstall:  // 文件删除前执行（Inno 无 usBeforeUninstall，实测报错后修正）
      begin
        SauExe := ExpandConstant('{app}\sau.exe');
        // §13.1-1：先礼后兵（任务 #3）——命名事件通知托盘优雅退出，防幽灵托盘图标；
        // 旧版无该命令时事件不存在、秒返回退出码 0，行为等同现状；随后 taskkill 兜底。
        // 显式传 --timeout 15（修复 2）：探测已前移、最坏延迟 ≤1 个 POLL_INTERVAL，
        // 15s 窗口留足余量，避免贴边超时退回 taskkill。
        if FileExists(SauExe) then
          ExecHide(SauExe, 'tray-exit --timeout 15');
        ExecHide('taskkill.exe', '/f /im sau.exe');
        // §13.1-2：停服务（等待最多 30s，由 service stop 内部窗口控制）
        if FileExists(SauExe) then
        begin
          ExecHide(SauExe, 'service stop');
          // §13.1-3：remove + sc delete 兜底
          ExecHide(SauExe, 'service remove');
          ExecHide('sc.exe', 'delete {#MyServiceName}');
        end;
        // 评审修复 #4：删除有头登录残留计划任务 SAU\HeadedLogin-*（服务已停，
        // 兜底清理；失败静默容忍）。
        DeleteHeadedLoginTasks();
      end;
    usPostUninstall:
      begin
        // §13.1-4：删当前用户自启项（§13.3：不跨用户清理，接受现状）
        RegDeleteValue(HKEY_CURRENT_USER,
          'Software\Microsoft\Windows\CurrentVersion\Run', 'SAUTray');
        // 任务 #3：凭证无条件删除，不受 DeleteLocalData 开关控制，
        // 且先于整删执行，逐个校验确保删净（重装不得复用旧 Token）。
        DeleteCredentialFile(ExpandConstant('{#MyDataDir}\credential.bin'));
        DeleteCredentialFile(ExpandConstant('{#MyDataDir}\local_token.bin'));
        // §13.1-6（任务 #3）：卸载前弹框确认过才整删数据目录（默认保留）
        if DeleteLocalData then
          DelTree(ExpandConstant('{#MyDataDir}'), True, True, True);
      end;
  end;
end;
