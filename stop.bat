@echo off
title Stop OPC Auto
echo Stopping OPC Auto (Port 8088)...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8088"') do (
    taskkill /F /PID %%a >nul 2>&1
)
echo Service stopped.
pause
