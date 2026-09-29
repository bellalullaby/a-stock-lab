@echo off
REM ============================================================
REM run_briefing.bat - briefing + email entry (called by schtasks)
REM Usage: run_briefing.bat closing^|morning
REM   closing -> 15:40: closing_briefing.py + email --source closing
REM              (10min buffer after 15:30 collect, mtime guard passes)
REM   morning -> 07:35: morning_briefing.py + email --source morning
REM Idempotent lock: briefing scripts skip if already ran today (exit 0)
REM Failure must be loud: any non-zero step -> "! FAILED" mark in log
REM NOTE: keep this file ASCII-only. Chinese comments break GBK cmd parse.
REM NOTE: use %time% only (ASCII digits). %date% outputs GBK Chinese (weekday)
REM       on zh-CN systems, which corrupts daily_briefing.log encoding.
REM ============================================================
cd /d D:\MyClaude\a-stock-lab\virtual-trading-web

if "%~1"=="closing" goto closing
if "%~1"=="morning" goto morning
echo [%time%] [run_briefing] bad arg: %~1 (need closing or morning) >> daily_briefing.log
exit /b 1

:closing
echo [%time%] === closing briefing + email START === >> daily_briefing.log
D:\Python312\python.exe closing_briefing.py >> daily_briefing.log 2>&1
if errorlevel 1 goto fail
D:\Python312\python.exe email_briefing.py --source closing >> daily_briefing.log 2>&1
if errorlevel 1 goto fail
echo [%time%] closing OK >> daily_briefing.log
exit /b 0

:morning
echo [%time%] === morning briefing + email START === >> daily_briefing.log
D:\Python312\python.exe morning_briefing.py >> daily_briefing.log 2>&1
if errorlevel 1 goto fail
D:\Python312\python.exe email_briefing.py --source morning >> daily_briefing.log 2>&1
if errorlevel 1 goto fail
echo [%time%] morning OK >> daily_briefing.log
exit /b 0

:fail
echo [%time%] !!!!!!!! FAILED (%~1) !!!!! - check daily_briefing.log now >> daily_briefing.log
exit /b 1
