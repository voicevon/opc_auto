@echo off
title OPC Auto
start "" "http://localhost:8088"
python server.py
if errorlevel 1 pause
