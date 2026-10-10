' OPC Auto - 24小时后台静默守护启动脚本 (无控制台黑窗口)
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "python server.py", 0, False
Set WshShell = Nothing
