# bootstrap.ps1 - Software TV Tuner: Windows one-shot setup.
#
#   powershell -ExecutionPolicy Bypass -File bootstrap.ps1
#
# Checks every prerequisite, builds the decoder module, and finishes by
# running the install doctor. Two things it can't do for you (licenses /
# size): installing radioconda and Visual Studio Build Tools - for those
# it prints the exact command and stops. Safe to re-run.

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=== Software TV Tuner - Windows installer ===" -ForegroundColor Cyan

# -- 1. find radioconda ------------------------------------------------
$condaRoots = @(
    $env:CONDA_PREFIX,
    "$env:USERPROFILE\radioconda",
    "$env:LOCALAPPDATA\radioconda",
    "C:\radioconda"
) | Where-Object { $_ -and (Test-Path "$_\python.exe") }

if (-not $condaRoots) {
    Write-Host "[X] radioconda not found." -ForegroundColor Red
    Write-Host "    Install it (free; bundles GNU Radio + SoapySDR + Python):"
    Write-Host "      https://github.com/ryanvolz/radioconda/releases  (Windows x64 installer)"
    Write-Host "    then re-run this script."
    exit 1
}
$conda = $condaRoots[0]
$py = "$conda\python.exe"
Write-Host "[o] radioconda: $conda"

# put the driver DLL dirs on PATH for everything below
$env:PATH = "$conda;$conda\Library\bin;$conda\Scripts;C:\Program Files\SDRplay\API\x64;" + $env:PATH

# -- 2. Visual Studio Build Tools (C++ workload) -----------------------
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$haveVS = (Test-Path $vswhere) -and
          (& $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -latest -property installationPath)
if ($haveVS) {
    Write-Host "[o] Visual Studio C++ build tools found"
} else {
    Write-Host "[X] Visual Studio 2022 Build Tools (C++ workload) not found." -ForegroundColor Red
    Write-Host "    Install with:"
    Write-Host '      winget install Microsoft.VisualStudio.2022.BuildTools --override "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --passive"'
    Write-Host "    then re-run this script."
    exit 1
}

# -- 3. ffmpeg ----------------------------------------------------------
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Write-Host "[o] ffmpeg on PATH"
} elseif (Test-Path "C:\ffmpeg\bin\ffmpeg.exe") {
    $env:PATH = "C:\ffmpeg\bin;" + $env:PATH
    Write-Host "[o] ffmpeg at C:\ffmpeg\bin"
} else {
    Write-Host "[!] ffmpeg not found - recording/remux features need it." -ForegroundColor Yellow
    Write-Host "    Get a full build from https://www.gyan.dev/ffmpeg/builds/ and extract to C:\ffmpeg\"
}

# -- 4. build the decoder module ---------------------------------------
Write-Host "[.] building gr-atscplus (this is the slow step)..."
Push-Location "$here\gr-atscplus"
cmd /c "_build.bat"
$buildCode = $LASTEXITCODE
Pop-Location
if ($buildCode -ne 0) {
    Write-Host "[X] build failed (exit $buildCode) - scroll up for the first error." -ForegroundColor Red
    exit 1
}

# -- 5. player extras + verify ------------------------------------------
& $py -m pip install --quiet opencv-python sounddevice
Write-Host "[o] player extras installed"
Write-Host ""
& $py "$here\tools\doctor.py"
if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "=== Done. Run it: ===" -ForegroundColor Cyan
    Write-Host "  $py $here\tools\tv_tuner.py"
}
exit $LASTEXITCODE
