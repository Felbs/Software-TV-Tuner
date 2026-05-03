@echo off
setlocal
cd /d Z:\src\magic-tv-decoder\gr-atscplus

call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
if errorlevel 1 ( echo [build] vcvars64 failed & exit /b 1 )

set "PATH=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin;%PATH%"

call "%USERPROFILE%\radioconda\Scripts\activate.bat" "%USERPROFILE%\radioconda"

if exist build rmdir /s /q build
mkdir build
cd build

echo [build] Configuring with NMake against radioconda GR...
cmake .. -G "NMake Makefiles" ^
    -DCMAKE_BUILD_TYPE=Release ^
    -DCMAKE_PREFIX_PATH=%USERPROFILE%\radioconda\Library ^
    -DCMAKE_INSTALL_PREFIX=%USERPROFILE%\radioconda\Library
if errorlevel 1 ( echo [build] cmake configure failed & exit /b 1 )

echo [build] Building...
cmake --build . --config Release
if errorlevel 1 ( echo [build] build failed & exit /b 1 )

echo [build] Installing into radioconda...
cmake --install . --config Release
if errorlevel 1 ( echo [build] install failed & exit /b 1 )

echo [build] === Done ===
endlocal
