@echo off
setlocal

REM ---- Find a usable Python launcher (py preferred, python fallback) ----
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python not found. Install Python 3.10+ from python.org and check "Add to PATH".
    echo Or disable Windows "App execution aliases" for python/python3 in Settings.
    pause
    exit /b 1
)

REM ---- Verify Python actually runs (avoid Microsoft Store alias trap) ----
%PY% -c "import sys; sys.exit(0)" >nul 2>nul
if errorlevel 1 (
    echo Python alias is broken (Microsoft Store stub). Please install the official Python and check "Add to PATH".
    pause
    exit /b 1
)

cd /d "%~dp0"

REM ---- Create venv if missing or broken ----
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo Failed to create venv. Try running as administrator.
        pause
        exit /b 1
    )
)

REM ---- Install runtime deps only when something is actually missing ----
REM 必需包见 requirements.txt（pystray / Pillow）。每次都跑 pip 会联网，还会往
REM 项目根（OneDrive 同步目录）写一个 pip_install.log，所以先探测再装。
.venv\Scripts\python.exe -c "import pystray, PIL" >nul 2>nul
if errorlevel 1 (
    echo Installing dependencies...
    .venv\Scripts\python.exe -m pip install -r requirements.txt >pip_install.log 2>&1
    if errorlevel 1 (
        echo [error] pip install failed. See pip_install.log
        echo         Missing pystray/Pillow means no tray icon: closing the
        echo         window would quit the program instead of hiding to tray.
        pause
        exit /b 1
    )
)

REM ---- Qt6 界面依赖检查：提示用，不中止启动 ----
REM 界面是 Qt6，缺绑定时程序起不来。app.py 会弹原生提示框说明原因，这里先
REM 在控制台提一句，省得用户去翻日志。刻意不在这里跑 pip：venv 里的 pip
REM 本身可能是坏的，那样连"程序都打不开"就成了另一个问题。
set "QT_OK="
.venv\Scripts\python.exe -c "import PyQt6.QtCore" >nul 2>nul
if not errorlevel 1 set "QT_OK=1"
if not defined QT_OK (
    echo [warn] PyQt6 not found - the UI cannot start.
    echo        Install it:
    echo          .venv\Scripts\python.exe -m pip install PyQt6
)

REM ---- Clear stale crash log, then launch GUI ----
REM crash.log 写在 %APPDATA%\HexWand（见 hexwand/core/paths.py 的 CRASH_PATH）
set "CRASHLOG=%APPDATA%\HexWand\crash.log"
if exist "%CRASHLOG%" del /f "%CRASHLOG%"
.venv\Scripts\python.exe app.py
if exist "%CRASHLOG%" (
    echo.
    echo ===== Program crashed. crash.log contents: =====
    REM crash.log 是 UTF-8（app.py 写入时指定），控制台默认 GBK 会把中文打成乱码
    chcp 65001 >nul
    type "%CRASHLOG%"
    chcp 936 >nul
    pause
)

endlocal
