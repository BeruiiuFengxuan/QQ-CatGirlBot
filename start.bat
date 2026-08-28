@echo off
cd /d "%~dp0"

set "PYTHON="

set "CANDIDATES=python;py;%LOCALAPPDATA%\Programs\Python\Python311\python.exe;%LOCALAPPDATA%\Programs\Python\Python38\python.exe"
for %%P in (%CANDIDATES%) do (
  if not defined PYTHON (
    %%P -c "import botpy" >nul 2>nul
    if not errorlevel 1 set "PYTHON=%%P"
  )
)

if not defined PYTHON (
  echo No usable Python/dependencies found, forcing Python 3.11 install ...
  set "PY311=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  if not exist "%PY311%" (
    echo Downloading Python 3.11 ...
    curl -L -o python_installer.exe https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
    if exist python_installer.exe (
      python_installer.exe /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1
      del python_installer.exe
    )
  )
  set "PYTHON=%PY311%"
  echo Installing dependencies ...
  "%PYTHON%" -m pip install -r requirements.txt
  "%PYTHON%" -c "import botpy" >nul 2>nul
  if errorlevel 1 (
    echo Dependency install still failed. Check network or install Python 3.11 + qq-botpy manually.
    cmd /k
    exit /b 1
  )
)

echo Starting GuoNiang bot ...
"%PYTHON%" main.py
cmd /k
