@echo off
REM ============================================================
REM run_signal_tracker.bat — 信号追踪每日自动刷新
REM 被任务计划程序调用（每交易日 16:30）：从 portfolio.json 提取
REM 全部历史信号，拉 K 线算 D+1/3/5/10 表现，写 signal_tracker.json。
REM 依赖：收盘简报（15:35 左右）已把当日 signals 写入 daily_log。
REM ============================================================
cd /d D:\MyClaude\a-stock-lab\virtual-trading-web
D:\Python312\python.exe signal_tracker.py >> signal_tracker.log 2>&1
exit /b 0
