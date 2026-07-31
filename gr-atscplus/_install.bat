@echo off
REM Install-only companion to _rebuild.bat. `_rebuild.bat` BUILDS but does NOT
REM install — Python then keeps importing the stale .pyd (the standing gotcha).
REM Run this straight after any _rebuild.bat.
setlocal
cd /d "%~dp0"

call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
if errorlevel 1 ( echo [install] vcvars64 failed & exit /b 1 )

set "PATH=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin;%PATH%"

if not defined RADIOCONDA set "RADIOCONDA=%USERPROFILE%\radioconda"
call "%RADIOCONDA%\Scripts\activate.bat" "%RADIOCONDA%"

cd build
echo [install] cmake --install ...
cmake --install . --config Release
if errorlevel 1 ( echo [install] install failed ^(DLL locked? stop the panel / live chains^) & exit /b 1 )

echo [install] Syncing python bindings to the import path...
xcopy /Y /I /Q "%RADIOCONDA%\Library\Lib\site-packages\gnuradio\atscplus\*" "%RADIOCONDA%\Lib\site-packages\gnuradio\atscplus\" >nul
if errorlevel 1 ( echo [install] bindings sync failed & exit /b 1 )

echo [install] === Done ===
endlocal
