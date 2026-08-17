@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo    MikuAgent Windows 环境搭建
echo ============================================
echo.

rem ---- 1. 寻找可用的 Python ----
set "PYTHON_EXE="
if exist "D:\anaconda\envs\mikuchat\python.exe" set "PYTHON_EXE=D:\anaconda\envs\mikuchat\python.exe"
if not defined PYTHON_EXE if exist "D:\anaconda\python.exe" set "PYTHON_EXE=D:\anaconda\python.exe"
if not defined PYTHON_EXE if exist "%USERPROFILE%\miniconda3\python.exe" set "PYTHON_EXE=%USERPROFILE%\miniconda3\python.exe"
if not defined PYTHON_EXE if exist "%USERPROFILE%\anaconda3\python.exe" set "PYTHON_EXE=%USERPROFILE%\anaconda3\python.exe"
if not defined PYTHON_EXE if exist "C:\ProgramData\anaconda3\python.exe" set "PYTHON_EXE=C:\ProgramData\anaconda3\python.exe"
if not defined PYTHON_EXE if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYTHON_EXE (
    where python >nul 2>nul
    if not errorlevel 1 set "PYTHON_EXE=python"
)
if not defined PYTHON_EXE (
    echo [错误] 未找到 Python，请先安装 Python 3.10+ 或 Anaconda。
    pause
    exit /b 1
)

echo [1/4] 使用 Python: %PYTHON_EXE%
"%PYTHON_EXE%" --version

rem ---- 2. 创建虚拟环境 ----
if not exist ".venv\Scripts\python.exe" (
    echo [2/4] 创建虚拟环境 .venv ...
    "%PYTHON_EXE%" -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败。
        pause
        exit /b 1
    )
) else (
    echo [2/4] 虚拟环境已存在，跳过。
)

rem ---- 3. 安装依赖 ----
echo [3/4] 安装依赖（首次需要联网下载，请耐心等待）...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败。
    pause
    exit /b 1
)

rem ---- 4. 生成 .env ----
if not exist ".env" (
    echo [4/4] 生成 .env 配置文件...
    copy /y ".env.example" ".env" >nul
    echo.
    echo 请编辑 .env，填入你的 DEEPSEEK_API_KEY：
    echo 申请地址：https://platform.deepseek.com
) else (
    echo [4/4] .env 已存在，跳过。
)

echo.
echo 环境搭建完成！运行 start.bat 启动桌宠。
pause
