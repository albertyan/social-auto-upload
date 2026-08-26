; ============================================================
; sau.iss —— SAU 安装包（Inno Setup 6，实施计划 S8；重建方案 §7.3/§13/§17）
;
; 编译（/D 可覆盖版本与构建目录）：
;   ISCC /DMyAppVersion=2.0.0a0 sau_wrap\packaging\installer\sau.iss
;   → 产物 installer\Output\sau-{version}.exe（与升级链命名一致，§8.4）
;
; 覆盖场景（§7.3 编者注）：干净机器首次安装 + 新布局自身版本间覆盖升级。
; 卸载六步见 §13；安装失败矩阵见 §17；内核不在安装器内下载（§7.3 定案）。
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
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; §7.3 首装步骤 1：整目录递归释放 {app}（sau.exe + ui/ + 运行时依赖）
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; post-install 编排脚本（§17 失败不静默）
Source: "..\post_install.bat"; DestDir: "{app}"; Flags: ignoreversion

[Run]
; 任务 #26（托盘缺失修复）：sau.iss 原本无 [Run] 段，装完从不拉起托盘；
; post-install 异常中断时 HKCU 自启项也未写入 → 重启后托盘同样缺席。
; postinstall = 完成页勾选项（默认勾选）；nowait 不阻塞向导收尾；
; 静默安装跳过（托盘依赖交互会话）；托盘自身单实例 Mutex 兜底重复启动。
Filename: "{app}\sau.exe"; Parameters: "tray"; \
  Description: "启动 SAU 托盘（后台常驻，任务 #26）"; \
  Flags: postinstall nowait skipifsilent skipifdoesntexist

[Code]
var
  DeleteDataCheck: TNewCheckBox;

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

procedure CurStepChanged(CurStep: TSetupStep);
var
  ExitCode: Integer;
  Msg: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  // §7.3 步骤 2~6 由 post_install.bat 编排（含 §17 阶段 3/4/6 的明确报错）
  if not Exec('cmd.exe', '/c ""' + ExpandConstant('{app}\post_install.bat')
              + '" > "' + ExpandConstant('{app}\post_install.log') + '" 2>&1"',
              '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then
    ExitCode := 99;  // cmd.exe 未能启动：报未知码，不得静默放行（任务 #26）
  case ExitCode of
    0:  Exit;  // 成功（自启/状态佐证失败按 §17 阶段 6 记日志不阻断，bat 已内化）
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
end;

// ---------------- 卸载（§13 六步 + §13.2 数据保留勾选）----------------

procedure InitializeUninstallProgressForm();
begin
  // §13.2：勾选框「同时删除本地数据」，默认不勾（默认保留）
  DeleteDataCheck := TNewCheckBox.Create(UninstallProgressForm);
  with DeleteDataCheck do
  begin
    Parent := UninstallProgressForm;
    Left := ScaleX(16);
    Top := UninstallProgressForm.ClientHeight - ScaleY(40);
    Width := UninstallProgressForm.ClientWidth - ScaleX(32);
    Height := ScaleY(17);
    Caption := '同时删除本地数据（%ProgramData%\SAU：cookies/凭证/任务库，默认保留）';
    Checked := False;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  SauExe: String;
begin
  case CurUninstallStep of
    usUninstall:  // 文件删除前执行（Inno 无 usBeforeUninstall，实测报错后修正）
      begin
        SauExe := ExpandConstant('{app}\sau.exe');
        // §13.1-1：杀托盘进程
        ExecHide('taskkill.exe', '/f /im sau.exe');
        // §13.1-2：停服务（等待最多 30s，由 service stop 内部窗口控制）
        if FileExists(SauExe) then
        begin
          ExecHide(SauExe, 'service stop');
          // §13.1-3：remove + sc delete 兜底
          ExecHide(SauExe, 'service remove');
          ExecHide('sc.exe', 'delete {#MyServiceName}');
        end;
      end;
    usPostUninstall:
      begin
        // §13.1-4：删当前用户自启项（§13.3：不跨用户清理，接受现状）
        RegDeleteValue(HKEY_CURRENT_USER,
          'Software\Microsoft\Windows\CurrentVersion\Run', 'SAUTray');
        // §13.1-6 + §13.2：勾选才删数据目录（默认保留）
        if (DeleteDataCheck <> nil) and DeleteDataCheck.Checked then
          DelTree(ExpandConstant('{#MyDataDir}'), True, True, True);
      end;
  end;
end;
