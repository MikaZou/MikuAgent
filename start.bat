@echo off
chcp 65001 >nul
title MikuAgent - 初音未来桌宠
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
echo    MikuAgent 初音未来原生桌宠 启动中...
echo    无边框 / 透明 / 置顶，可拖动
echo    右键托盘图标可打开设置或退出
echo ============================================
echo.

rem pythonw：无控制台窗口启动。必须用绝对路径，相对路径 Start-Process 会解析失败。
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0main.py"