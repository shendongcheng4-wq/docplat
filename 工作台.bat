@echo off
rem Start the review workbench on http://127.0.0.1:7788  (ASCII only: cmd decodes as GBK)
cd /d "%~dp0"
call plat.bat serve --port 7788 %*
pause
