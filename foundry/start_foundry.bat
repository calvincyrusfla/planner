@echo off
rem start_foundry.bat - run the Foundry service in this window (Windows).
rem
rem   foundry\start_foundry.bat                  serve the repository this script lives in
rem   foundry\start_foundry.bat C:\code\myrepo   serve another repository
rem   foundry\start_foundry.bat --repo C:\code\myrepo
rem
rem Runs: .venv\Scripts\python -m foundry.serve --repo ^<path^>
rem The service listens on http://127.0.0.1:4310 (FOUNDRY_SERVE_PORT).
rem Stop it with Ctrl+C or by closing this window.
rem FOUNDRY_DRY_RUN=1 prints the command instead of running it.

setlocal EnableExtensions
title Foundry

for %%I in ("%~dp0..") do set "REPO_ROOT=%%~fI"
set "PY=%REPO_ROOT%\.venv\Scripts\python.exe"
set "TARGET=%REPO_ROOT%"

if /i "%~1"=="--repo" (
  if "%~2"=="" goto :usage
  set "TARGET=%~f2"
  goto :args_done
)
if /i "%~1"=="-h" goto :help
if /i "%~1"=="--help" goto :help
if not "%~1"=="" set "TARGET=%~f1"
:args_done
rem Normalise: drop a trailing backslash ("C:\My Repo\" -> "C:\My Repo"); a drive root
rem becomes "C:\." so the launched --repo "..." never ends in \" (cmd would read \" as a quote).
for %%I in ("%TARGET%\.") do set "TARGET=%%~fI"
if "%TARGET:~-1%"=="\" set "TARGET=%TARGET%."

if not exist "%PY%" goto :no_venv
if not exist "%TARGET%\" goto :no_repo

set "PORT=4310"
if defined FOUNDRY_SERVE_PORT set "PORT=%FOUNDRY_SERVE_PORT%"
cd /d "%REPO_ROOT%"
echo [foundry] Repo:   "%TARGET%"
echo [foundry] Python: "%PY%"
echo [foundry] Starting the Foundry service on http://127.0.0.1:%PORT% ...
echo [foundry] Stop it with Ctrl+C, or close this window.
echo.

if "%FOUNDRY_DRY_RUN%"=="1" goto :dry_run

:run
"%PY%" -m foundry.serve --repo "%TARGET%"
set "RC=%ERRORLEVEL%"
if "%RC%"=="75" goto :restart
echo.
echo [foundry] Foundry stopped (exit code %RC%).
goto :hold

:restart
echo.
echo [foundry] Restarting with the updated code...
echo.
goto :run

:dry_run
echo [dry-run] "%PY%" -m foundry.serve --repo "%TARGET%"
set "RC=0"
goto :end

:no_venv
echo [foundry] No Python virtual environment found at:
echo [foundry]   "%PY%"
echo [foundry] Create it from the repository root first - see "Install" in foundry\README.md:
echo [foundry]   py -3.11 -m venv .venv
echo [foundry]   .venv\Scripts\python -m pip install -r foundry\requirements.txt
set "RC=1"
goto :hold

:no_repo
echo [foundry] Repository folder not found: "%TARGET%"
set "RC=1"
goto :hold

:usage
echo Usage: foundry\start_foundry.bat [--repo] [path]
echo   path defaults to the repository root: "%REPO_ROOT%"
set "RC=2"
goto :hold

:help
echo Usage: foundry\start_foundry.bat [--repo] [path]
echo   path defaults to the repository root: "%REPO_ROOT%"
set "RC=0"
goto :end

:hold
if "%FOUNDRY_DRY_RUN%"=="1" goto :end
echo.
pause

:end
endlocal & exit /b %RC%
