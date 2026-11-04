@echo off
rem docplat launcher. Keep this file ASCII + CRLF.
rem cmd decodes batch files with the console codepage (936 on zh-CN Windows),
rem so a UTF-8 bat containing a Chinese path turns into mojibake and
rem "if not exist" never matches.
setlocal
cd /d "%~dp0"

rem python resolution order:
rem   1) .venv next to this script
rem   2) python_path.txt (one line, git-ignored; for a venv living elsewhere)
rem   3) DOCPLAT_PY environment variable
rem   4) python on PATH
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" if exist "%~dp0python_path.txt" set /p PY=<"%~dp0python_path.txt"
if not exist "%PY%" set "PY=%DOCPLAT_PY%"
if not exist "%PY%" set "PY=python"

"%PY%" -m docplat %*
