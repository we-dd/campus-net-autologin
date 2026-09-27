@echo off
cd /d "%~dp0"

echo ============================================
echo   校园网自动登录 - 手动测试
echo ============================================
echo.
echo 跑的是 --check，和开机自启走的完全同一条路径。
echo.
echo   已经有网：什么都不做，直接退出，不消耗登录额度。
echo   没有网：弹出 Chrome，自动填学号密码、认验证码、点登录。
echo.
echo 整个过程大概 20-40 秒，请不要手动关那个 Chrome 窗口。
echo.
pause

echo.
echo ---- 开始 ----
python login.py --check --verbose
set RC=%ERRORLEVEL%
echo.
echo ---- 结束，退出码 %RC% ----
echo.

if "%RC%"=="0" echo 结果：成功，或本来就在线
if "%RC%"=="1" echo 结果：登录失败，请看 failure-*.html 和 failure-*.png
if "%RC%"=="2" echo 结果：配置或环境错误

echo.
echo 完整日志在 campus-net.log
echo.
pause
