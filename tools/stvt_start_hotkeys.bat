@echo off
REM Click once at startup. Leaves a small console running that listens for:
REM   Ctrl+Shift+E -> switch chain audio to English
REM   Ctrl+Shift+S -> switch chain audio to Spanish
REM   Ctrl+Shift+A -> cycle EN -> ES -> ALL
REM   Ctrl+Shift+Q -> stop the listener

setlocal
set "PY=%USERPROFILE%\radioconda\python.exe"
"%PY%" -u "%~dp0stvt_hotkey_listener.py"
endlocal
