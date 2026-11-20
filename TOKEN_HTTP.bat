@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo PCBridge Python environment missing. Run START.bat first.
  pause
  exit /b 1
)
echo PCBridge 1.7.0 - local URL-token Streamable HTTP on 127.0.0.1:8770
echo Connect using: http://127.0.0.1:8770/^(token from TOKEN.txt^)/mcp
".venv\Scripts\python.exe" -u "server\token_http.py"
if errorlevel 1 pause
