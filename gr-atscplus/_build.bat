@echo off
setlocal
cd /d "%~dp0"

REM Find vcvars64 for ANY Visual Studio edition via vswhere (Build Tools,
REM Community, Professional...). Falls back to the classic Build Tools path.
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
set "VSROOT="
if exist "%VSWHERE%" for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -latest -property installationPath`) do set "VSROOT=%%i"
if not defined VSROOT set "VSROOT=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools"
call "%VSROOT%\VC\Auxiliary\Build\vcvars64.bat"
if errorlevel 1 ( echo [build] vcvars64 failed & exit /b 1 )

set "PATH=%VSROOT%\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin;%PATH%"

REM radioconda location: override with RADIOCONDA env if not in the default spot
if not defined RADIOCONDA set "RADIOCONDA=%USERPROFILE%\radioconda"
call "%RADIOCONDA%\Scripts\activate.bat" "%RADIOCONDA%"

if not exist "%RADIOCONDA%\Library\include\Eigen" if not exist "%RADIOCONDA%\Library\include\eigen3\Eigen" (
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
    -DCMAKE_PREFIX_PATH=%RADIOCONDA%\Library ^
    -DCMAKE_INSTALL_PREFIX=%RADIOCONDA%\Library
if errorlevel 1 ( echo [build] cmake configure failed & exit /b 1 )

echo [build] Building...
cmake --build . --config Release
if errorlevel 1 ( echo [build] build failed & exit /b 1 )

echo [build] Installing into radioconda...
cmake --install . --config Release
if errorlevel 1 ( echo [build] install failed & exit /b 1 )

REM CMake installs Python bindings to Library\Lib\site-packages\, but Python
REM actually imports from %RADIOCONDA%\Lib\site-packages\. Mirror
REM the freshly-built module over so `from gnuradio import atscplus` picks it up.
echo [build] Syncing Python bindings to env site-packages...
xcopy /Y /I /Q "%RADIOCONDA%\Library\Lib\site-packages\gnuradio\atscplus\*" "%RADIOCONDA%\Lib\site-packages\gnuradio\atscplus\" >nul
if errorlevel 1 ( echo [build] python-bindings sync failed & exit /b 1 )

echo [build] === Done ===
endlocal
