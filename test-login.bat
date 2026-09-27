@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

echo ============================================
echo   校园网自动登录 - 手动测试
echo ============================================
echo.
echo 前提：现在应该是"没网"的状态。
echo 如果还有网，先拔网线 10 秒再插回去。
echo.
echo 跑的是 --check（探测 + 按需登录），和开机自启时走的完全一样：
echo   * 已经有网  -^> 它什么都不做，直接退出（安全，不会白试登录）
echo   * 没有网    -^> 弹出 Chrome，自动填学号密码、认验证码、点登录
echo.
echo 整个过程大概 20-40 秒，请不要手动关那个 Chrome 窗口。
echo.
echo 按任意键开始...
pause >nul

echo.
echo ---- 开始 ----
python login.py --check --verbose
set RC=%ERRORLEVEL%
echo ---- 结束（退出码 %RC%）----
echo.

if "%RC%"=="0" (
  echo 结果：成功（或本来就在线）
) else if "%RC%"=="1" (
  echo 结果：登录失败 —— 如果目录里多了 failure-*.html / failure-*.png，把它们发给我
) else (
  echo 结果：配置或环境错误
)

echo.
echo 完整日志在 campus-net.log
echo.
echo ============================================
echo   按任意键关闭本窗口
echo ============================================
pause >nul
