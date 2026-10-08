@echo off
rem One-click launcher for the daily margin-trading report.
rem Usage: Start.bat [--no-send] [--from-csv] [--log-level DEBUG]
chcp 65001 > nul
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run: uv sync
    pause
    exit /b 1
)
.\.venv\Scripts\python.exe -m eastmoneyrzrq %*
set EXIT_CODE=%ERRORLEVEL%
if not "%EXIT_CODE%"=="0" (
    echo [ERROR] exit code: %EXIT_CODE%
)
pause
exit /b %EXIT_CODE%
