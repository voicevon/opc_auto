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
echo [说明] Chrome 136+ 禁止在默认 Profile 上开启 CDP 调试端口，
echo        故使用项目专用持久化 Profile (data\chrome_cdp_profile)，登录态永久保存。
start "" %CHROME_PATH% --remote-debugging-port=9222 --user-data-dir=c:\software\opc_auto\data\chrome_cdp_profile "https://buyertrade.taobao.com/trade/itemlist/list_bought_items.htm"

echo.
echo [就绪] 浏览器已拉起并开启 9222 端口！
echo -> 首次使用请用手机淘宝扫码登录一次，此后该 Profile 永久免登录。
echo -> 保持该浏览器窗口开启，系统在定时或手动核验时将自动接管采集。
echo ======================================================================
timeout /t 5 >nul
