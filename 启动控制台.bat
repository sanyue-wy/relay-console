@echo off
chcp 65001 >nul
title Relay Console
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [Relay Console] 未找到 .venv，请先按 README 完成安装：
  echo   python -m venv .venv
  echo   .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)
echo [Relay Console] 启动中… 保持本窗口打开（它就是服务进程，关闭=停止控制台）。
echo 带 token 的控制台地址见下方输出，也会写进 console_url.txt。
".venv\Scripts\python.exe" run_console.py
pause
