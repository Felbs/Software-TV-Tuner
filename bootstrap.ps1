# bootstrap.ps1 - Software TV Tuner: Windows one-shot setup.
#
#   powershell -ExecutionPolicy Bypass -File bootstrap.ps1               # check + build
#   powershell -ExecutionPolicy Bypass -File bootstrap.ps1 -AutoInstall  # also installs missing prerequisites
#   powershell -ExecutionPolicy Bypass -File bootstrap.ps1 -CheckOnly    # just report, change nothing
#
# With -AutoInstall this is the whole Windows install on a fresh PC:
# radioconda (downloaded from its GitHub releases), Visual Studio Build
# Tools + ffmpeg (via winget), Eigen (via conda), then the decoder build
# and the install doctor. Safe to re-run; each step skips if satisfied.

param(
    [switch]$AutoInstall,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=== Software TV Tuner - Windows installer ===" -ForegroundColor Cyan

function Find-Radioconda {
    @(
        $env:RADIOCONDA,
        $env:CONDA_PREFIX,
        "$env:USERPROFILE\radioconda",
        "$env:LOCALAPPDATA\radioconda",
        "C:\radioconda"
    ) | Where-Object { $_ -and (Test-Path "$_\python.exe") } | Select-Object -First 1
}

# -- 1. radioconda -------------------------------------------------------
$conda = Find-Radioconda
if (-not $conda -and $AutoInstall -and -not $CheckOnly) {
    Write-Host "[.] downloading radioconda (latest release)..."
    $rel = Invoke-RestMethod https://api.github.com/repos/ryanvolz/radioconda/releases/latest
    $asset = $rel.assets | Where-Object { $_.name -match 'Windows-x86_64\.exe$' } | Select-Object -First 1
    if (-not $asset) { Write-Host "[X] no Windows installer asset found" -ForegroundColor Red; exit 1 }
    $exe = "$env:TEMP\$($asset.name)"
    Invoke-WebRequest $asset.browser_download_url -OutFile $exe
    Write-Host "[.] installing radioconda silently to $env:USERPROFILE\radioconda ..."
    Start-Process $exe -ArgumentList "/InstallationType=JustMe /AddToPath=0 /RegisterPython=0 /S /D=$env:USERPROFILE\radioconda" -Wait
    $conda = Find-Radioconda
}
if (-not $conda) {
    Write-Host "[X] radioconda not found." -ForegroundColor Red
    Write-Host "    Re-run with -AutoInstall, or install it yourself:"
    Write-Host "      https://github.com/ryanvolz/radioconda/releases  (Windows x64 installer)"
    exit 1
}
$py = "$conda\python.exe"
Write-Host "[o] radioconda: $conda"
$env:RADIOCONDA = $conda
$env:PATH = "$conda;$conda\Library\bin;$conda\Scripts;C:\Program Files\SDRplay\API\x64;" + $env:PATH

# -- 2. Visual Studio Build Tools (C++ workload) --------------------------
function Test-VSTools {
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    (Test-Path $vswhere) -and
    (& $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -latest -property installationPath)
}
if (Test-VSTools) {
    Write-Host "[o] Visual Studio C++ build tools found"
} elseif ($AutoInstall -and -not $CheckOnly) {
    Write-Host "[.] installing VS 2022 Build Tools via winget (big download, be patient)..."
    winget install Microsoft.VisualStudio.2022.BuildTools --accept-source-agreements --accept-package-agreements --override "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --passive --wait"
    if (-not (Test-VSTools)) {
        Write-Host "[X] build tools still not detected - a reboot or new shell may be needed." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "[X] Visual Studio 2022 Build Tools (C++ workload) not found." -ForegroundColor Red
    Write-Host "    Re-run with -AutoInstall, or install with:"
    Write-Host '      winget install Microsoft.VisualStudio.2022.BuildTools --override "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --passive"'
    exit 1
}

# -- 3. ffmpeg -------------------------------------------------------------
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Write-Host "[o] ffmpeg on PATH"
} elseif (Test-Path "C:\ffmpeg\bin\ffmpeg.exe") {
    $env:PATH = "C:\ffmpeg\bin;" + $env:PATH
    Write-Host "[o] ffmpeg at C:\ffmpeg\bin"
} elseif ($AutoInstall -and -not $CheckOnly) {
    Write-Host "[.] installing ffmpeg via winget..."
    winget install Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
    $env:PATH = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:PATH
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) { Write-Host "[o] ffmpeg installed" }
    else { Write-Host "[!] ffmpeg installed but not on this shell's PATH yet - fine after a new shell" -ForegroundColor Yellow }
} else {
    Write-Host "[!] ffmpeg not found - recording/remux features need it (re-run with -AutoInstall)" -ForegroundColor Yellow
}

# -- 4. Eigen headers (decoder build dependency) ----------------------------
$haveEigen = (Test-Path "$conda\Library\include\Eigen") -or (Test-Path "$conda\Library\include\eigen3\Eigen")
if ($haveEigen) {
    Write-Host "[o] Eigen headers present"
} elseif (-not $CheckOnly) {
    Write-Host "[.] installing Eigen into radioconda..."
    $condaExe = @("$conda\Scripts\conda.exe", "$conda\condabin\conda.bat") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($condaExe) { & $condaExe install -y -q -c conda-forge eigen=3.4.0 }
    else { Write-Host "[X] conda executable not found in $conda" -ForegroundColor Red; exit 1 }
} else {
    Write-Host "[!] Eigen headers missing (a normal run installs them via conda)" -ForegroundColor Yellow
}

if ($CheckOnly) {
    Write-Host ""
    & $py "$here\tools\doctor.py"
    exit $LASTEXITCODE
}

# -- 5. build the decoder module -------------------------------------------
Write-Host "[.] building gr-atscplus (this is the slow step)..."
Push-Location "$here\gr-atscplus"
cmd /c "_build.bat"
$buildCode = $LASTEXITCODE
Pop-Location
if ($buildCode -ne 0) {
    Write-Host "[X] build failed (exit $buildCode) - scroll up for the first error." -ForegroundColor Red
    exit 1
}

# -- 6. player extras + verify ----------------------------------------------
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
