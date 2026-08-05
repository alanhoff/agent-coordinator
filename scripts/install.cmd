@echo off
setlocal EnableExtensions
set "SOURCE="
set "PROJECT=."

:parse
if "%~1"=="" goto parsed
if /I "%~1"=="--source" goto source
if /I "%~1"=="--source-url" goto source
if /I "%~1"=="--project" goto project
if /I "%~1"=="-h" goto help
if /I "%~1"=="--help" goto help
echo Unknown argument: %~1 1>&2
exit /b 2

:source
if "%~2"=="" (
  echo Missing value for %~1 1>&2
  exit /b 2
)
set "SOURCE=%~2"
shift
shift
goto parse

:project
if "%~2"=="" (
  echo Missing value for --project 1>&2
  exit /b 2
)
set "PROJECT=%~2"
shift
shift
goto parse

:help
echo Usage: install.cmd --source https://host/skills/coordinator [--project PATH]
exit /b 0

:parsed
if "%SOURCE%"=="" (
  echo --source https://host/skills/coordinator is required 1>&2
  exit /b 2
)
if "%SOURCE:~-1%"=="/" set "SOURCE=%SOURCE:~0,-1%"
where python >nul 2>nul || (
  echo python is required and must be on PATH 1>&2
  exit /b 20
)
where git >nul 2>nul || (
  echo git is required and must be on PATH 1>&2
  exit /b 20
)

set "INSTALLER="
for /f "usebackq delims=" %%I in (`python -c "import os,tempfile;fd,p=tempfile.mkstemp(prefix='coordinator-install-',suffix='.py');os.close(fd);print(p)"`) do set "INSTALLER=%%I"
if "%INSTALLER%"=="" (
  echo Could not allocate a temporary installer path 1>&2
  exit /b 20
)
python -c "import pathlib,sys,urllib.request;u=sys.argv[1];p=pathlib.Path(sys.argv[2]);r=urllib.request.Request(u,headers={'User-Agent':'coordinator-install-cmd/2'});p.write_bytes(urllib.request.urlopen(r,timeout=30).read())" "%SOURCE%/scripts/install.py" "%INSTALLER%"
if errorlevel 1 (
  del /q "%INSTALLER%" >nul 2>nul
  exit /b 20
)
python "%INSTALLER%" install --source-url "%SOURCE%" --project "%PROJECT%" --json
set "RESULT=%ERRORLEVEL%"
del /q "%INSTALLER%" >nul 2>nul
exit /b %RESULT%
