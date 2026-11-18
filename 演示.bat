@echo off
cd /d "%~dp0"
echo.
echo   织流 AgentWeave - 终端现场演示（实时事件旁白）
echo   约 1-2 秒跑完一次完整的「编排-执行-验证-自循环-报告」
echo.
where py >nul 2>nul
if %errorlevel%==0 (
  py -3.14 scripts/demo_tour.py
) else (
  python scripts/demo_tour.py
)
echo.
echo 演示结束。按任意键关闭窗口。
pause >nul
