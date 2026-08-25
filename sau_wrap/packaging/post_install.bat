@echo off
rem ============================================================
rem post_install.bat - SAU post-install orchestration
rem (design doc section 7.3 steps 2~5 / section 17 failure matrix)
rem Invoked by Inno Setup sau.iss at ssPostInstall, stdout/stderr
rem redirected to {app}\post_install.log. Exit codes:
rem    0  success (autostart write failure tolerated, stage 6)
rem    11 service install failed   (stage 3: never swallow)
rem    12 service start failed     (stage 4: retried twice)
rem Pure ASCII on purpose: cmd.exe parses .bat with the ANSI code
rem page; UTF-8 CJK text would break parsing on CP936 machines.
rem ============================================================
setlocal EnableExtensions
set "APPDIR=%~dp0"
set "SAU=%APPDIR%sau.exe"
set "DATA=%ProgramData%\SAU"

echo [post-install] %date% %time% APPDIR=%APPDIR%

rem ---- section 7.3 step 2: create runtime data dirs (section 3.6) ----
for %%D in (cookies db logs etc browsers downloads updates) do (
    if not exist "%DATA%\%%D" mkdir "%DATA%\%%D"
)

rem ---- users-full ACL (tray / user session processes need write) ----
icacls "%DATA%" /grant *S-1-5-32-545:(OI)(CI)F /T /C
if errorlevel 1 echo [post-install][warn] icacls failed (continue)

rem ---- write {app}\VERSION (content from sau.exe --version) ----
"%SAU%" --version > "%APPDIR%VERSION" 2>nul
if errorlevel 1 echo [post-install][warn] VERSION write failed (continue)

rem ---- section 7.3 step 4: register service (delayed-auto + failure
rem      restart policy handled inside "service install") ----
echo [post-install] registering service...
"%SAU%" service install
if errorlevel 1 (
    echo [post-install][FAIL] service install failed (stage 3, not swallowed)
    exit /b 11
)

rem ---- section 7.3 step 5: start service, retry twice at 10s (stage 4) ----
set /a ATTEMPT=0
:start_retry
set /a ATTEMPT+=1
echo [post-install] starting service, attempt %ATTEMPT%/3 ...
"%SAU%" service start
if not errorlevel 1 goto started_ok
if %ATTEMPT% GEQ 3 (
    echo [post-install][FAIL] service start failed (stage 4, retries exhausted)
    exit /b 12
)
echo [post-install][warn] start failed, retry in 10s...
timeout /t 10 /nobreak >nul
goto start_retry

:started_ok
rem ---- verify status once more (leave evidence in the log) ----
"%SAU%" service status

rem ---- section 7.3 step 3: autostart for the CURRENT user (section 5.4);
rem      failure tolerated per stage 6 (log only, tray can start manually) ----
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v SAUTray /t REG_SZ /d "\"%SAU%\" tray" /f
if errorlevel 1 (
    echo [post-install][warn] autostart registry write failed (stage 6: log only)
)

echo [post-install] done
exit /b 0
