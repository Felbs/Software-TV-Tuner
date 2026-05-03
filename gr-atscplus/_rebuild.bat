@echo off
setlocal
cd /d Z:\src\magic-tv-decoder\gr-atscplus

call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
if errorlevel 1 ( echo [build] vcvars64 failed & exit /b 1 )

set "PATH=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin;%PATH%"

call "%USERPROFILE%\radioconda\Scripts\activate.bat" "%USERPROFILE%\radioconda"

cd build
echo [rebuild] Incremental build...
cmake --build . --config Release
if errorlevel 1 ( echo [rebuild] build failed & exit /b 1 )

echo [rebuild] === Done ===
endlocal
