@echo off
setlocal
TITLE One-Click Starter for social-auto-upload

REM 为什么要优先使用项目 .venv 下的解释器：
REM 项目使用 uv 管理依赖，完整依赖（patchright、pystray、opencv-python 等）
REM 均安装在 .venv\Scripts\python.exe；若直接用系统/Anaconda 的 python 会因为环境隔离导致 ModuleNotFoundError。
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if exist "%VENV_PY%" (
    REM 虚拟环境存在则优先用虚拟环境的 python 则用它
    set "PYEXE=%VENV_PY%"
) else (
    REM 兜底：没虚拟环境则回退到 PATH 里的 python
    set "PYEXE=python"
)

ECHO ==================================================
ECHO  Starting social-auto-upload Servers...
ECHO ==================================================
ECHO  Using Python: %PYEXE%
ECHO ==================================================
ECHO.

ECHO [1/2] Starting Python Backend Server in a new window...
START "SAU Backend" cmd /k ""%PYEXE%" sau_backend.py"

ECHO [2/2] Starting Vue.js Frontend Server in another new window...
START "SAU Frontend" cmd /k "cd sau_frontend && npm run dev -- --host 0.0.0.0"

ECHO.
ECHO ==================================================
ECHO  Done.
ECHO  Two new windows have been opened for the backend
ECHO  and frontend servers. You can monitor logs there.
ECHO ==================================================
ECHO.

ECHO This window will close in 10 seconds...
timeout /t 10 /nobreak > nul

endlocal
