"""exclusive_tv.py — run a TV experiment with EXCLUSIVE use of the radio.

The 7/29-30 lesson, learned twice: the passive instruments (prop_atlas,
storm_watch) and the radio panel open the SDR directly, so a cooperative
warden hold is not enough — a TV decode started alongside them gets a few
milliseconds of samples and dies. This wrapper stops those daemons, runs
the command, then restarts exactly the ones it stopped.

    python lab/exclusive_tv.py <cmd...>
"""
import subprocess
import sys
import time

PS = ["powershell", "-NoProfile", "-Command"]
DAEMONS = {
    "prop_atlas": ["powershell", "-ExecutionPolicy", "Bypass", "-File",
                   r"Z:\src\gr-radiotuna\tools\atlas_start.ps1"],
    "storm_watch": ["powershell", "-ExecutionPolicy", "Bypass", "-File",
                    r"Z:\src\gr-radiotuna\tools\storm_start.ps1"],
    "radio_panel": [r"C:\Users\user\radioconda\python.exe",
                    r"Z:\src\gr-radiotuna\tools\radio_panel.py"],
}


def running():
    out = subprocess.run(PS + [
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Select-Object -ExpandProperty CommandLine"],
        capture_output=True, text=True).stdout
    return {k for k in DAEMONS if k in out}


def stop(names):
    for n in names:
        subprocess.run(PS + [
            f"Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            f"Where-Object {{$_.CommandLine -match '{n}'}} | ForEach-Object "
            f"{{ Stop-Process -Id $_.ProcessId -Force -Confirm:$false }}"],
            capture_output=True)
    subprocess.run(PS + ["Restart-Service -Name SDRplayAPIService -Force "
                         "-Confirm:$false"], capture_output=True, timeout=120)
    time.sleep(12)


def start(names):
    for n in sorted(names):
        cmd = DAEMONS[n]
        cwd = r"Z:\src\gr-radiotuna\tools" if n == "radio_panel" else None
        subprocess.Popen(cmd, cwd=cwd,
                         creationflags=subprocess.CREATE_NO_WINDOW,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        time.sleep(3)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    was = running()
    print(f"[excl] stopping for exclusive radio: {sorted(was)}", flush=True)
    stop(was)
    try:
        rc = subprocess.run(sys.argv[1:], cwd=r"Z:\src\magic-tv-decoder").returncode
    finally:
        print(f"[excl] restoring: {sorted(was)}", flush=True)
        start(was)
    print(f"[excl] done rc={rc}", flush=True)
    raise SystemExit(rc)
