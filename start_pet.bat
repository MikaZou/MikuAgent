@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [提示] 请先运行 setup_windows.bat 完成环境搭建。
    pause
    exit /b 1
)

echo 正在以「透明置顶桌宠窗口」模式启动（pywebview）...
echo 提示：如果未安装 pywebview，请先运行 setup_windows.bat 安装依赖。
start "" cmd /k ".venv\Scripts\python.exe backend\pet_window.py"
