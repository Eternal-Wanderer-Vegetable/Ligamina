@echo off
setlocal

if exist E:\stella\Ligamina\AGENTS.md exit /b 20
if defined LIGAMINA_HOST_SECRET_TEST exit /b 21
if /i "%USERNAME%"=="WDAGUtilityAccount" exit /b 26

echo unexpected>C:\Ligamina\Input\worker-write-probe.txt 2>nul
if exist C:\Ligamina\Input\worker-write-probe.txt exit /b 22

echo {"status":"passed"}>C:\ProgramData\Ligamina\Outbound\spoof.json 2>nul
if exist C:\ProgramData\Ligamina\Outbound\spoof.json exit /b 23

where.exe curl.exe >nul 2>nul
if errorlevel 1 exit /b 24
curl.exe --connect-timeout 3 --max-time 4 -sS https://1.1.1.1/ >nul 2>nul
if not errorlevel 1 exit /b 25

echo windows-sandbox-access-boundaries-passed
exit /b 0
