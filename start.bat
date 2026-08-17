@echo off
chcp 65001 >nul
title MikuAgent - 初音未来虚拟桌宠
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [提示] 未检测到虚拟环境，正在执行环境搭建...
    call setup_windows.bat
    if errorlevel 1 exit /b 1
)

if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo [提示] 已生成 .env，请编辑并填入 DEEPSEEK_API_KEY 后重启以获得完整 AI 能力。
)

echo.
echo ============================================
echo    MikuAgent 初音未来虚拟桌宠 启动中...
echo    访问地址: http://127.0.0.1:8000
echo ============================================
echo.

start "MikuAgent Backend" cmd /k ".venv\Scripts\python.exe -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000"
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:8000"
