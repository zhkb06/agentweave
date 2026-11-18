@echo off
cd /d "%~dp0"
echo.
echo   织流 AgentWeave - 多智能体编排、调度与可信验证平台
echo   正在启动 Web 控制台 ...
echo.
where py >nul 2>nul
if %errorlevel%==0 (
  py -3.14 server.py --open-browser
) else (
  python server.py --open-browser
)
echo.
echo 服务已退出。按任意键关闭窗口。
pause >nul
