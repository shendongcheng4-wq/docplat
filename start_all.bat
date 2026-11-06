@echo off
rem Start GLM engine + review workbench. Keep this file ASCII + CRLF:
rem cmd decodes batches as CP936, a UTF-8 file with a Chinese path breaks.
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" if exist "%~dp0python_path.txt" set /p PY=<"%~dp0python_path.txt"
if not exist "%PY%" set "PY=%DOCPLAT_PY%"
if not exist "%PY%" set "PY=python"

echo [1/2] GLM engine   http://127.0.0.1:8081
start "docplat-glm" "%ComSpec%" /k ""%PY%" scripts\glm_serve.py"
echo [2/2] workbench    http://127.0.0.1:7788
timeout /t 3 >nul
"%PY%" -m docplat serve --port 7788

pause
