@echo off
chcp 65001 >nul
title OPC Auto - Chrome CDP 调试端口启动器
echo ======================================================================
echo   OPC Auto 宿主真实浏览器接管启动器 (复用本地登录 Cookie)
echo ======================================================================
echo.
echo [提示] 本脚本将以 9222 调试端口拉起真实 Chrome 浏览器，供拟人化采集器接管。
echo [注意] 若您当前已有普通的 Chrome 窗口打开，请先保存并关闭它，
echo        然后按任意键继续启动调试实例！
echo.
pause

set CHROME_PATH="C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist %CHROME_PATH% set CHROME_PATH="C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if not exist %CHROME_PATH% set CHROME_PATH="C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

echo 正在启动浏览器: %CHROME_PATH%
start "" %CHROME_PATH% --remote-debugging-port=9222 --restore-last-session "https://buyertrade.taobao.com/trade/itemlist/list_bought_items.htm"

echo.
echo [就绪] 浏览器已拉起并开启 9222 端口！
echo -> 请在打开的浏览器中确保已登录淘宝/京东账号，并打开『已买到的宝贝』订单列表页。
echo -> 保持该浏览器窗口开启，系统在定时或手动核验时将自动接管采集。
echo ======================================================================
timeout /t 5 >nul
