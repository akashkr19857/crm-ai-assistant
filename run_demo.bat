@echo off
REM CRM AI Assistant one-click demo (Windows). Starts the API on :8300 and the dashboard on :3300.
REM Needs: Ollama running with qwen2.5:7b and nomic-embed-text pulled; Node 22; Python 3.11 with requirements.txt.
setlocal
cd /d "%~dp0"
set "PY=%~dp0..\_shared\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [1/4] Checking Ollama models...
ollama list | findstr /i "qwen2.5:7b" >nul || ollama pull qwen2.5:7b
ollama list | findstr /i "nomic-embed-text" >nul || ollama pull nomic-embed-text

echo [2/4] Demo CRM data + knowledge documents (first run only)...
if not exist "data\manifest.json" "%PY%" data\make_docs.py
"%PY%" backend\scripts\seed.py

echo [3/4] Starting API on http://localhost:8300 ...
start "CRM Assistant API" /D "%~dp0backend" "%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8300

echo [4/4] Starting dashboard on http://localhost:3300 ...
cd frontend
if not exist node_modules call npm install
if not exist .next\BUILD_ID call npm run build
start "CRM Assistant dashboard" /D "%~dp0frontend" cmd /k npm run start
timeout /t 6 >nul
start http://localhost:3300
endlocal
