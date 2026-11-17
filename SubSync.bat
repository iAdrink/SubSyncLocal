@echo off
cd /d "%~dp0"
set PYW=
where pythonw >nul 2>nul && set PYW=pythonw
if "%PYW%"=="" where pyw >nul 2>nul && set PYW=pyw -3
if "%PYW%"=="" (
  echo No encuentro Python. Instalalo desde python.org y marca "Add Python to PATH".
  pause
  exit /b 1
)
start "" %PYW% "%~dp0SubSyncLocal.pyw"
exit
