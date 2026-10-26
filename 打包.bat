@echo off
chcp 65001 >nul
setlocal
REM build.py 会把 maturin 的输出原样打印，里面有 ✏️ 这类字符：只 chcp 不够，
REM Python 的 stdout 编码仍按系统 ANSI(GBK) 走，2026-09-18 实测崩在打印那步。
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

echo ============================================
echo   HexWand Auto Build
echo ============================================
echo.

if not exist "D:\rc_build_venv\Scripts\python.exe" (
    echo [ERROR] Build env missing: D:\rc_build_venv
    echo         Create a venv with system Python 3.14 and install
    echo         pyinstaller / pystray / Pillow / maturin first.
    echo         (maturin 负责编译 rust/ 原生扩展 hexwand_rust)
    echo.
    pause
    exit /b 1
)

"D:\rc_build_venv\Scripts\python.exe" build.py
if errorlevel 1 (
    echo.
    echo [FAILED] Build aborted. See messages above.
    pause
    exit /b 1
)

echo.
echo Build OK. New exe: dist\HexWand.exe
echo.
pause
endlocal
