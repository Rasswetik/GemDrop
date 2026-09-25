@echo off
setlocal
where py >nul 2>&1
if errorlevel 1 (
  echo Не найден запускатель Python ^(py^). Установите Python для Windows и повторите попытку.
  echo https://www.python.org/downloads/windows/
  exit /b 1
)
py -3 "%~dp0upload_to_github.py" %*
exit /b %errorlevel%
