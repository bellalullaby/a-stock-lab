@echo off
REM ============================================================
REM start_flask_guard.bat — A股虚拟盘 Flask 幂等守护启动器
REM 被任务计划程序调用（登录时 + 每 10 分钟）：
REM   - 5000 端口已在监听 → 直接退出（幂等，不重复启动）
REM   - 未在监听 → 后台最小化拉起 Flask，日志写 flask.log
REM ============================================================
netstat -ano 2>nul | findstr /C:":5000" | findstr /C:"LISTENING" >nul 2>&1
if not errorlevel 1 exit /b 0

cd /d D:\MyClaude\a-stock-lab\virtual-trading-web
start "AStockFlask" /min cmd /c "D:\Python312\python.exe app.py >> flask.log 2>&1"
exit /b 0
