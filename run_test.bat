@echo off
setlocal
cd /d "%~dp0"
title AutoVNC Local Navigation Test

echo ========================================================
echo  AutoVNC Local Machine Tester
echo ========================================================
echo.

set PLATFORM=diskwala
if not "%~1"=="" set PLATFORM=%~1

echo Running: node test_autovnc_local.js %PLATFORM% %2
echo.
node test_autovnc_local.js %PLATFORM% %2

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo --------------------------------------------------------
    echo [ERROR] Test script exited with error code %ERRORLEVEL%.
    echo --------------------------------------------------------
)

echo.
pause
