@echo off
REM Start SoapySDRServer on Windows so WSL2 (or any other host on the
REM LAN) can use the SDR over TCP. WSL2's usbipd-win drops 80%+ of
REM isochronous USB transfers, so for SDR streaming the only reliable
REM path is to keep the SDR on Windows and stream samples over TCP
REM loopback. The Hyper-V NAT bridge between Windows and WSL2 happily
REM moves multi-gigabit/sec on localhost.
REM
REM Prerequisites (one-time):
REM   1. Install radioconda (or any SoapySDR distribution that bundles
REM      SoapyRemote). Most do.
REM   2. If the SDR is currently attached to WSL, detach it so Windows
REM      can open it:    usbipd detach --busid <BUSID>
REM
REM Usage:
REM   tools\soapy_server.bat
REM
REM Then from WSL2:
REM   ip route show default | awk '{print $3}'   -> the Windows host IP
REM   python3 tools/probe_throughput.py \
REM       --soapy-args "driver=remote,remote=<host_ip>:55132,remote:driver=sdrplay"

setlocal

set "SOAPY_SERVER=%USERPROFILE%\radioconda\Library\bin\SoapySDRServer.exe"
if not exist "%SOAPY_SERVER%" (
    echo [soapy_server] %SOAPY_SERVER% not found.
    echo [soapy_server] Install radioconda or set SOAPY_SERVER env var.
    exit /b 1
)

REM Re-enable the SDRplay API path so SoapySDRPlay can find the .dll.
set "PATH=C:\Program Files\SDRplay\API\x64;%PATH%"

echo [soapy_server] starting SoapySDRServer on 0.0.0.0:55132
echo [soapy_server]   (any host on your LAN can connect; bind 127.0.0.1
echo [soapy_server]    only if you want to restrict to local clients)
echo.
REM Bind explicitly to 0.0.0.0:55132 (all IPv4 interfaces). The bare
REM ":55132" form silently binds to ONE IPv6 interface only — clients
REM connecting via IPv4 (e.g. WSL2 over the Hyper-V NAT bridge) then
REM time out. 0.0.0.0 catches all IPv4 traffic.
"%SOAPY_SERVER%" --bind="0.0.0.0:55132"

endlocal
