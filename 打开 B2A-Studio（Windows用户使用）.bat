@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

set "ROOT=%~dp0"
set "APP=%ROOT%B2A-Studio"
set "B2A_PORT=8501"
set "B2A_URL=http://127.0.0.1:%B2A_PORT%/"
set "B2A_LOG=%APP%\logs\streamlit.launch.log"

if not exist "%APP%\app.py" (
  echo [错误] 未找到 B2A-Studio 目录，请确认本脚本与 B2A-Studio 文件夹在同一目录下。
  pause
  exit /b 1
)

cd /d "%APP%"
if not exist "logs" mkdir "logs"

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
  echo.
  echo [错误] 未检测到 Python。请安装 Python 3.9 或更高版本，并勾选「Add Python to PATH」。
  echo 下载：https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

echo [%date% %time%] 启动检查 ^(%PY%^) >> "%B2A_LOG%"

%PY% -c "import streamlit" 2>nul
if errorlevel 1 (
  echo.
  echo 首次运行：正在安装 requirements.txt 依赖，请稍候…
  echo.
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo [错误] 依赖安装失败。请手动执行：
    echo   cd /d "%APP%"
    echo   %PY% -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
  )
)

%PY% -c "import mutagen" 2>nul
if errorlevel 1 (
  echo 正在安装 mutagen（MP3 歌词内嵌 / LRC 导出）…
  %PY% -m pip install "mutagen==1.47.0"
)

netstat -ano 2>nul | findstr /C:":%B2A_PORT% " | findstr /I "LISTENING" >nul
if not errorlevel 1 (
  echo B2A-Studio 似乎已在运行，正在打开浏览器…
  start "" "%B2A_URL%"
  pause
  exit /b 0
)

echo.
echo 正在后台启动 B2A-Studio（Streamlit）…
start "B2A-Studio" /MIN cmd /c "%PY% -m streamlit run app.py --server.port %B2A_PORT% --server.headless true --browser.gatherUsageStats false >> \"%B2A_LOG%\" 2>&1"

set "READY=0"
for /L %%i in (1,1,35) do (
  timeout /t 1 /nobreak >nul
  powershell -NoProfile -Command "try { (Invoke-WebRequest -Uri '%B2A_URL%' -UseBasicParsing -TimeoutSec 2).StatusCode | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
  if not errorlevel 1 (
    set "READY=1"
    goto server_ready
  )
)

echo.
echo [警告] 服务启动超时。请稍后手动打开：%B2A_URL%
echo 日志：%B2A_LOG%
pause
exit /b 1

:server_ready
start "" "%B2A_URL%"
echo.
echo ========================================
echo   B2A-Studio 已在后台运行
echo   网页：%B2A_URL%
echo   日志：%B2A_LOG%
echo.
echo   · 关闭任务栏中最小化的「B2A-Studio」窗口可结束服务
echo   · 或在网页侧边栏点击「完全退出」
echo ========================================
echo.
pause
exit /b 0
