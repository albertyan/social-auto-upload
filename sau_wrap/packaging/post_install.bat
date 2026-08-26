@echo off
rem ============================================================
rem post_install.bat - SAU post-install orchestration
rem (design doc section 7.3 steps 2~5 / section 17 failure matrix)
rem Invoked by Inno Setup sau.iss at ssPostInstall, stdout/stderr
rem redirected to {app}\post_install.log.
rem
rem Exit code matrix (task #26: closed matrix, every path explicit,
rem no stray command may leak an unmapped code to the installer):
rem    0  success (status-evidence / autostart failure tolerated: warn only)
rem    11 service install failed  (stage 3: never swallowed)
rem    12 service start failed    (stage 4: retried twice)
rem cmd.exe itself fails to launch -> Inno side maps to 99 (unknown).
rem
rem Task #26 fixes:
rem - every external step captured via %%ERRORLEVEL%% + echoed to the log
rem   (previous version could die silently after service install success);
rem - NO bare parentheses inside any echo text: cmd block-depth parsing
rem   miscounts them when a false "if (...)" block is scanned, which can
rem   execute a wrong line (verified: caused spurious exit /b 11);
rem - "timeout /t" replaced by "ping -n" (timeout.exe fails in Session 0 /
rem   redirected stdin environments);
rem - timestamp via PowerShell ISO format (pure digits, no locale weekday
rem   CJK chars -> no mojibake); chcp 65001 keeps the whole log UTF-8
rem   (sau.exe entry hardening outputs utf-8; this file stays pure ASCII);
rem - SAU_DATA_ROOT override for test isolation (production: ProgramData).
rem ============================================================
chcp 65001 >nul
setlocal EnableExtensions
set "APPDIR=%~dp0"
set "SAU=%APPDIR%sau.exe"
rem SAU_EXE / SAU_DATA_ROOT: test injection seams (verify_s8; production leaves unset)
if defined SAU_EXE set "SAU=%SAU_EXE%"
if defined SAU_DATA_ROOT (set "DATA=%SAU_DATA_ROOT%") else set "DATA=%ProgramData%\SAU"

set "TS=unknown"
for /f "delims=" %%T in ('powershell -NoProfile -Command "Get-Date -Format s"') do set "TS=%%T"
echo [post-install] %TS% APPDIR=%APPDIR% DATA=%DATA%

rem ---- section 7.3 step 2: create runtime data dirs (section 3.6) ----
for %%D in (cookies db logs etc browsers downloads updates) do (
    if not exist "%DATA%\%%D" mkdir "%DATA%\%%D"
)

rem ---- users-full ACL (tray / user session processes need write) ----
icacls "%DATA%" /grant *S-1-5-32-545:(OI)(CI)F /T /C
if errorlevel 1 echo [post-install][warn] icacls failed [continue]

rem ---- write {app}\VERSION (content from sau.exe --version) ----
rem "call" keeps control flow if SAU_EXE test seam points at a .cmd shim
rem (production: sau.exe PE, call is a harmless no-op passthrough)
call "%SAU%" --version > "%APPDIR%VERSION" 2>nul
if errorlevel 1 echo [post-install][warn] VERSION write failed [continue]

rem ---- section 7.3 step 4: register service (delayed-auto + failure
rem      restart policy handled inside "service install"; task #26:
rem      already-exists is idempotent success inside sau.exe) ----
echo [post-install] registering service...
call "%SAU%" service install
set "RC=%ERRORLEVEL%"
echo [post-install] service install exit=%RC%
if not "%RC%"=="0" (
    echo [post-install][FAIL] service install failed [stage 3, exit=%RC%, not swallowed]
    exit /b 11
)

rem ---- section 7.3 step 5: start service, retry twice at 10s (stage 4) ----
set /a ATTEMPT=0
:start_retry
set /a ATTEMPT+=1
echo [post-install] starting service, attempt %ATTEMPT%/3 ...
call "%SAU%" service start
set "RC=%ERRORLEVEL%"
echo [post-install] service start exit=%RC% attempt %ATTEMPT%/3
if "%RC%"=="0" goto started_ok
if %ATTEMPT% GEQ 3 (
    echo [post-install][FAIL] service start failed [stage 4, retries exhausted]
    exit /b 12
)
echo [post-install][warn] start failed, retry in 10s...
ping -n 11 127.0.0.1 >nul
goto start_retry

:started_ok
rem ---- verify status once more (evidence step: failure tolerated) ----
call "%SAU%" service status
if errorlevel 1 echo [post-install][warn] service status reported anomaly [evidence only, not blocking]

rem ---- task #26 decision change: browser kernel auto-download at install
rem      time (design doc section 7.3 revised). Failure NEVER blocks the
rem      install (section 17 stage 5); already-installed is skipped inside
rem      sau.exe (upgrade does not re-download the ~295MB two-component
rem      pair); total cap 20 minutes enforced inside sau.exe; progress
rem      lands in this log and in
rem      %ProgramData%\SAU\logs\browser_install.log. ----
echo [post-install] browser kernel download starting [skip if installed]...
call "%SAU%" browser install
set "RC=%ERRORLEVEL%"
echo [post-install] browser install exit=%RC%
if not "%RC%"=="0" (
    echo [post-install][warn] browser kernel download failed [stage 5: not blocking, see browser_install.log]
)

rem ---- section 7.3 step 3: autostart for the CURRENT user (section 5.4);
rem      failure tolerated per stage 6 (log only, tray can start manually) ----
rem "call" keeps control flow even if "reg" resolves to a script in tests
rem (production: reg.exe PE, call is a harmless no-op passthrough)
call reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v SAUTray /t REG_SZ /d "\"%SAU%\" tray" /f
if errorlevel 1 (
    echo [post-install][warn] autostart registry write failed [stage 6: log only]
)

echo [post-install] done
exit /b 0
