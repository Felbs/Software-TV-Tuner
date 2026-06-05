@echo off
REM Double-click this file to toggle the running STVT chain between English
REM and Spanish audio. Detects which RF + program is currently playing and
REM respawns it with the other language. Works from anywhere — no shell
REM env vars required, the chain's winning config is set inside.

setlocal
set "STVT_REPO=%~dp0.."
set "PY=%USERPROFILE%\radioconda\python.exe"

if not exist "%PY%" (
    echo radioconda Python not found at %PY%
    echo Adjust the PY path at the top of this file or install radioconda.
    pause
    exit /b 1
)

"%PY%" "%STVT_REPO%\tools\stvt_audio_lang.py" toggle
endlocal
