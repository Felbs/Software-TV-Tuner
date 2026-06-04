@echo off
REM Click this to switch the running STVT chain to Spanish audio.
setlocal
set "PY=%USERPROFILE%\radioconda\python.exe"
"%PY%" "%~dp0stvt_audio_lang.py" spa
endlocal
