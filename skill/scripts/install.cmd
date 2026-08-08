@echo off
setlocal EnableExtensions
where python >nul 2>nul || (
  echo Python 3.11+ is required 1>&2
  exit /b 20
)
python "%~dp0install.py" %*
exit /b %ERRORLEVEL%
