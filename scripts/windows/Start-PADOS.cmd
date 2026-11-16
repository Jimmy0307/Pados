@echo off
rem SPDX-License-Identifier: AGPL-3.0-only
rem Run from cmd.exe or a shortcut: passes every argument to Start-PADOS.ps1 under Windows PowerShell 5.1.
rem Examples: Start-PADOS.cmd    Start-PADOS.cmd -Status    Start-PADOS.cmd -Stop    Start-PADOS.cmd -DryRun
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-PADOS.ps1" %*
set "PADOS_LAUNCHER_EXIT=%ERRORLEVEL%"
endlocal & exit /b %PADOS_LAUNCHER_EXIT%
