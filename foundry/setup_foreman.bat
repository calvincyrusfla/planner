@echo off
rem setup_foreman.bat - get the Gemma Shop Foreman (Ollama) ready on Windows.
rem
rem   foundry\setup_foreman.bat
rem
rem Steps (safe to run again, no admin rights needed):
rem   1. check that ollama is on PATH (else print the download link)
rem   2. report GPU / VRAM (nvidia-smi, else PowerShell Get-CimInstance; wmic is deprecated)
rem   3. pick the model: %FOREMAN_MODEL%, default gemma4:e4b (suggest gemma4:e2b below 8 GB VRAM)
rem   4. start "ollama serve" in the background if http://localhost:11434 does not answer
rem   5. "ollama pull" the model unless it is already installed
rem   6. run "python -m foundry.foreman_probe" with the repo's python (.venv, py -3 or python)
rem   7. run "python -m foundry.gemma_doctor" (GPU %% and tokens/s, standard library only).
rem      A slow or CPU-only Gemma is reported, not treated as a failed setup.
rem   8. report whether the GitHub CLI (gh) is installed and logged in, for automatic
rem      pull requests. Read-only ("gh auth status"); never fails the setup.
rem
rem Environment:
rem   FOREMAN_MODEL        model tag (default gemma4:e4b)
rem   FOREMAN_OLLAMA_URL   Ollama server root (default http://localhost:11434)
rem   FOREMAN_DRY_RUN=1    print the planned commands, never run ollama or the probe
rem   FOREMAN_WAIT_S       seconds to wait for "ollama serve" to answer (default 30)

setlocal EnableExtensions DisableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "REPO_ROOT=%%~fI"

set "MODEL=gemma4:e4b"
if defined FOREMAN_MODEL set "MODEL=%FOREMAN_MODEL%"
set "OLLAMA_URL=http://localhost:11434"
if defined FOREMAN_OLLAMA_URL set "OLLAMA_URL=%FOREMAN_OLLAMA_URL%"
if "%OLLAMA_URL:~-1%"=="/" set "OLLAMA_URL=%OLLAMA_URL:~0,-1%"
if /i "%OLLAMA_URL:~-3%"=="/v1" set "OLLAMA_URL=%OLLAMA_URL:~0,-3%"
set "DRY=0"
if "%FOREMAN_DRY_RUN%"=="1" set "DRY=1"
set "WAIT_S=30"
if defined FOREMAN_WAIT_S set /a "WAIT_S=FOREMAN_WAIT_S+0"
set "SERVE_LOG=%TEMP%\ollama-serve.log"
set "TMPF=%TEMP%\foreman_setup_%RANDOM%%RANDOM%"
set "RC=0"

rem ---- 1. ollama on PATH ----------------------------------------------------
where ollama >nul 2>&1
if not errorlevel 1 goto :have_ollama
rem A fresh install may not be on this console's PATH yet: try the default per-user location.
if not exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" goto :no_ollama
set "PATH=%LOCALAPPDATA%\Programs\Ollama;%PATH%"
:have_ollama
for /f "delims=" %%P in ('where ollama 2^>nul') do (
  echo [foreman] ollama found: %%P
  goto :gpu
)

rem ---- 2. GPU / VRAM --------------------------------------------------------
:gpu
set "VRAM_MB="
where nvidia-smi >nul 2>&1
if errorlevel 1 goto :gpu_cim
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits > "%TMPF%.gpu" 2>nul
if errorlevel 1 goto :gpu_cim
for /f "usebackq tokens=1,2 delims=," %%A in ("%TMPF%.gpu") do call :gpu_line "%%A" "%%B"
del "%TMPF%.gpu" >nul 2>&1
if defined VRAM_MB goto :gpu_done

:gpu_cim
where powershell >nul 2>&1
if errorlevel 1 goto :gpu_done
echo [foreman] Video adapters:
powershell -NoProfile -Command "Get-CimInstance Win32_VideoController | Select Name,AdapterRAM | Format-Table -AutoSize | Out-String -Width 200" 2>nul
rem AdapterRAM is a 32-bit field (caps at 4 GB); the display-class registry key has the real size
rem (readable without admin). Take the largest value either source reports, in MB.
powershell -NoProfile -Command "$r = @((Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0*' -ErrorAction SilentlyContinue).'HardwareInformation.qwMemorySize') + @((Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue).AdapterRAM); $a = @(foreach ($v in $r) { if ($null -ne $v) { try { [long]$v } catch {} } }); if ($a.Count) { [long](($a | Measure-Object -Maximum).Maximum / 1MB) } else { 0 }" > "%TMPF%.vram" 2>nul
set "VRAM_RAW="
set "VRAM_NUM=0"
if exist "%TMPF%.vram" set /p VRAM_RAW=<"%TMPF%.vram"
del "%TMPF%.vram" >nul 2>&1
if not defined VRAM_RAW goto :gpu_done
set /a "VRAM_NUM=VRAM_RAW+0" >nul 2>&1
if "%VRAM_NUM%"=="0" goto :gpu_done
set "VRAM_MB=%VRAM_NUM%"
echo [foreman] Largest GPU memory reported: %VRAM_MB% MB

:gpu_done
if not defined VRAM_MB echo [foreman] No GPU VRAM reading available; Ollama will fall back to CPU if needed.

rem ---- 3. model -------------------------------------------------------------
echo [foreman] Model: %MODEL% - set FOREMAN_MODEL to change it
if not defined VRAM_MB goto :serve
if %VRAM_MB% GEQ 8192 goto :serve
if /i not "%MODEL%"=="gemma4:e4b" goto :serve
echo [foreman] Only %VRAM_MB% MB VRAM, below 8 GB: consider "set FOREMAN_MODEL=gemma4:e2b" for a smaller, faster Foreman.

rem ---- 4. ollama serve ------------------------------------------------------
:serve
call :api_up
if not errorlevel 1 goto :api_ready
if "%DRY%"=="1" goto :serve_dry
echo [foreman] Ollama API not answering at %OLLAMA_URL%; starting "ollama serve" in the background.
echo [foreman] Log: %SERVE_LOG%
echo [foreman] NOTE: closing this window stops the "ollama serve" started here.
echo [foreman]       For a server that stays up, start the Ollama app instead - it runs in the system tray.
start "" /B ollama serve > "%SERVE_LOG%" 2>&1
set /a "WAITED=0"
:wait_loop
call :api_up
if not errorlevel 1 goto :api_started
if %WAITED% GEQ %WAIT_S% goto :serve_timeout
rem ping as a 1 s sleep: "timeout" fails when stdin is redirected.
ping -n 2 127.0.0.1 >nul 2>&1
set /a "WAITED+=1"
goto :wait_loop

:serve_timeout
echo [foreman] Ollama did not answer at %OLLAMA_URL% after %WAIT_S% s. See %SERVE_LOG%.
set "RC=1"
goto :finish

:serve_dry
echo [foreman] Ollama API not answering at %OLLAMA_URL%; would start it:
echo [dry-run] start "" /B ollama serve ^> "%SERVE_LOG%" 2^>^&1
echo [foreman] NOTE: closing this window would stop that "ollama serve"; the Ollama tray app keeps it running.
goto :pull

:api_started
echo [foreman] Ollama API is up at %OLLAMA_URL%
goto :pull

:api_ready
echo [foreman] Ollama API already answering at %OLLAMA_URL%

rem ---- 5. ollama pull -------------------------------------------------------
:pull
if not "%DRY%"=="1" goto :pull_real
echo [dry-run] ollama pull %MODEL%
goto :probe
:pull_real
ollama list 2>nul | findstr /b /i /c:"%MODEL% " /c:"%MODEL%:latest " >nul
if errorlevel 1 goto :do_pull
echo [foreman] Model %MODEL% already installed; skipping pull.
goto :probe
:do_pull
echo [foreman] Pulling %MODEL% - the first time can take a while...
ollama pull %MODEL%
if not errorlevel 1 goto :probe
echo [foreman] "ollama pull %MODEL%" failed. Check the tag and your connection, then run again.
set "RC=1"
goto :finish

rem ---- 6. probe -------------------------------------------------------------
:probe
rem First of .venv / py -3 / python that really runs Python 3.11 or newer
rem (running it, not "where python", also skips the Microsoft Store alias stub).
set "PY="
set "PY_IS_VENV=0"
if not exist "%REPO_ROOT%\.venv\Scripts\python.exe" goto :try_py_launcher
"%REPO_ROOT%\.venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set PY="%REPO_ROOT%\.venv\Scripts\python.exe"
if defined PY set "PY_IS_VENV=1"
if defined PY goto :run_probe
:try_py_launcher
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto :run_probe
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto :run_probe
echo [foreman] Python 3.11 or newer not found. Install it from https://www.python.org/downloads/ then run:
echo [foreman]   py -3 -m foundry.foreman_probe
set "RC=1"
goto :finish

:run_probe
cd /d "%REPO_ROOT%"
if not "%DRY%"=="1" goto :run_probe_real
echo [dry-run] %PY% -m foundry.foreman_probe --base-url "%OLLAMA_URL%/v1" --model "%MODEL%"
call :doctor
goto :next_step
:run_probe_real
%PY% -m foundry.foreman_probe --base-url "%OLLAMA_URL%/v1" --model "%MODEL%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :finish
echo [foreman] Shop Foreman ready.
rem ---- 7. speed check -------------------------------------------------------
call :doctor
:next_step
rem ---- 8. GitHub CLI --------------------------------------------------------
call :gh_check
if "%PY_IS_VENV%"=="1" goto :next_step_venv
rem The probe ran without a usable .venv; the orchestrator needs pydantic, so set up the venv first.
echo [foreman] No .venv yet - the probe ran with %PY%. Create the venv first, from %REPO_ROOT%
echo [foreman] - see "Install" in foundry\README.md:
echo   py -3.11 -m venv .venv
echo   .venv\Scripts\python -m pip install -r foundry\requirements.txt
echo [foreman] Then launch the orchestrator:
echo   .venv\Scripts\python -m foundry.main --real --repo ^<path^> --title "..."
goto :finish
:next_step_venv
echo [foreman] Next, from %REPO_ROOT%:
echo   %PY% -m foundry.main --real --repo ^<path^> --title "..."
goto :finish

:no_ollama
echo [foreman] Ollama is not installed - no "ollama" on PATH.
echo [foreman] Download it from https://ollama.com/download, install it, then run this script again.
set "RC=1"
goto :finish

rem ---- helpers --------------------------------------------------------------

rem :doctor - step 7, the speed check (GPU offload, tokens/s). Never changes RC:
rem exit 1 (slow / CPU) still means the Foreman works, with heuristic fallbacks.
:doctor
echo [foreman] Speed check: is Gemma on the GPU, and how fast? (python -m foundry.gemma_doctor)
if not "%DRY%"=="1" goto :doctor_real
echo [dry-run] %PY% -m foundry.gemma_doctor --base-url "%OLLAMA_URL%" --model "%MODEL%"
exit /b 0
:doctor_real
%PY% -m foundry.gemma_doctor --base-url "%OLLAMA_URL%" --model "%MODEL%"
set "DOC_RC=%ERRORLEVEL%"
if "%DOC_RC%"=="0" exit /b 0
if "%DOC_RC%"=="1" goto :doctor_slow
if "%DOC_RC%"=="2" goto :doctor_unreachable
echo [foreman] The speed check stopped with exit code %DOC_RC%. Run it again later: %PY% -m foundry.gemma_doctor
exit /b 0
:doctor_slow
echo [foreman] Gemma works, but slowly or on the CPU. Setup still succeeded: Foundry falls back to its
echo [foreman] built-in sizing when Gemma is too slow. To speed it up see "Make Gemma fast" in foundry\README.md.
exit /b 0
:doctor_unreachable
echo [foreman] The speed check could not reach Gemma or find the model. Run it again later:
echo [foreman]   %PY% -m foundry.gemma_doctor --model %MODEL%
exit /b 0

rem :gh_check - step 8: is the GitHub CLI installed and logged in (automatic pull requests)?
rem Read-only ("gh auth status", also in dry-run). Never changes RC.
:gh_check
echo [foreman] GitHub check: automatic pull requests need the GitHub CLI - gh - logged in.
where gh >nul 2>&1
if not errorlevel 1 goto :gh_found
rem A fresh winget install may not be on this console's PATH yet: try the default location.
if not exist "%ProgramFiles%\GitHub CLI\gh.exe" goto :gh_missing
set "PATH=%ProgramFiles%\GitHub CLI;%PATH%"
:gh_found
for /f "delims=" %%G in ('where gh 2^>nul') do (
  echo [foreman] gh found: %%G
  goto :gh_auth
)
:gh_auth
gh auth status >nul 2>&1
if errorlevel 1 goto :gh_logged_out
echo [foreman] gh is logged in: Foundry opens a pull request for every approved task.
exit /b 0
:gh_logged_out
echo [foreman] gh is not logged in. Run: gh auth login
echo [foreman] Until then Foundry still writes patches, but cannot open pull requests.
exit /b 0
:gh_missing
echo [foreman] gh is not installed. Foundry still writes patches, but cannot open pull requests.
echo [foreman] Install it with: winget install --id GitHub.cli
echo [foreman] then open a new window and run: gh auth login
echo [foreman] Or turn automatic pull requests off: set FOUNDRY_AUTO_PR=0
exit /b 0

rem :gpu_line "name" "memory MB" - print one nvidia-smi row and keep the largest VRAM.
:gpu_line
set "GNAME=%~1"
set "GMEM_RAW=%~2"
set "GMEM=0"
set /a "GMEM=GMEM_RAW+0" >nul 2>&1
echo [foreman] GPU: %GNAME% - %GMEM% MB VRAM
if "%GMEM%"=="0" exit /b 0
if not defined VRAM_MB set "VRAM_MB=%GMEM%"
if %GMEM% GTR %VRAM_MB% set "VRAM_MB=%GMEM%"
exit /b 0

rem :api_up - errorlevel 0 when the Ollama API answers (plain HTTP GET; never runs ollama).
:api_up
where curl.exe >nul 2>&1
if errorlevel 1 goto :api_up_ps
curl.exe -fsS -m 2 --noproxy "*" "%OLLAMA_URL%/api/version" >nul 2>&1
exit /b %ERRORLEVEL%
:api_up_ps
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri '%OLLAMA_URL%/api/version' | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
exit /b %ERRORLEVEL%

:finish
rem Keep the window open when the script was double-clicked from Explorer.
if "%DRY%"=="1" goto :end
echo %CMDCMDLINE% | findstr /i /c:"%~nx0" >nul 2>&1
if not errorlevel 1 pause
:end
endlocal & exit /b %RC%
