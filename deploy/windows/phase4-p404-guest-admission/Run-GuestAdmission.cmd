@echo off
setlocal
echo logon_command_started=%DATE% %TIME%> C:\P404Results\logon-command.log
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\P404Package\Collect-GuestAdmission.ps1 -OutputDirectory C:\P404Results >> C:\P404Results\logon-command.log 2>&1
set "collector_exit=%ERRORLEVEL%"
echo collector_exit=%collector_exit%>> C:\P404Results\logon-command.log
exit /b %collector_exit%
