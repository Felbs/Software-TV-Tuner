@echo off
setlocal
cd /d "%~dp0"

call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
if errorlevel 1 ( echo [build] vcvars64 failed & exit /b 1 )

set "PATH=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin;%PATH%"

call "%USERPROFILE%\radioconda\Scripts\activate.bat" "%USERPROFILE%\radioconda"

if not exist "%USERPROFILE%\radioconda\Library\include\Eigen" if not exist "%USERPROFILE%\radioconda\Library\include\eigen3\Eigen" (
    echo [build] Eigen3 headers missing -- required by atsc_equalizer_pilot.
    echo [build] Install with:  conda install -c conda-forge eigen=3.4.0
    exit /b 1
)

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

REM CMake installs Python bindings to Library\Lib\site-packages\, but Python
REM actually imports from %USERPROFILE%\radioconda\Lib\site-packages\. Mirror
REM the freshly-built module over so `from gnuradio import atscplus` picks it up.
echo [build] Syncing Python bindings to env site-packages...
xcopy /Y /I /Q "%USERPROFILE%\radioconda\Library\Lib\site-packages\gnuradio\atscplus\*" "%USERPROFILE%\radioconda\Lib\site-packages\gnuradio\atscplus\" >nul
if errorlevel 1 ( echo [build] python-bindings sync failed & exit /b 1 )

echo [build] === Done ===
endlocal
