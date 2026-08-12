"""exclusive_tv.py — run a TV experiment with EXCLUSIVE use of the radio.

The 7/29-30 lesson, learned twice: the passive instruments (prop_atlas,
storm_watch) and the radio panel open the SDR directly, so a cooperative
warden hold is not enough — a TV decode started alongside them gets a few
milliseconds of samples and dies. This wrapper stops those daemons, runs
the command, then restarts exactly the ones it stopped.

    python lab/exclusive_tv.py <cmd...>
"""
import os
import subprocess
import sys
import time

PS = ["powershell", "-NoProfile", "-Command"]
DAEMONS = {
    "prop_atlas": ["powershell", "-ExecutionPolicy", "Bypass", "-File",
                   r"Z:\src\gr-radiotuna\tools\atlas_start.ps1"],
    "storm_watch": ["powershell", "-ExecutionPolicy", "Bypass", "-File",
                    r"Z:\src\gr-radiotuna\tools\storm_start.ps1"],
    "radio_panel": [os.path.join(os.environ.get("USERPROFILE", ""), "radioconda", "python.exe"),
                    r"Z:\src\gr-radiotuna\tools\radio_panel.py"],
}


def running():
    out = subprocess.run(PS + [
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Select-Object -ExpandProperty CommandLine"],
        capture_output=True, text=True).stdout
    return {k for k in DAEMONS if k in out}


# warden owner names per daemon (radio_lock stop-file convention)
STOP_OWNERS = {"prop_atlas": ["prop_atlas"],
               "storm_watch": ["storm_watch"],
               "radio_panel": ["panel", "panel_idle"]}


def stop(names):
    """Graceful first (doctor law 8/01: hard-killing a streaming holder
    wedged the SDRplay API three times in 24 h): ask each daemon to wind
    down via the warden stop-file, wait up to 25 s, and only hard-kill
    the stragglers - followed by the service heal that such a kill makes
    necessary."""
    try:
        sys.path.insert(0, r"Z:\src\gr-radiotuna\tools")
        import radio_lock
        for n in names:
            for owner in STOP_OWNERS.get(n, []):
                radio_lock.request_stop(owner)
        deadline = time.time() + 25
        while running() & set(names) and time.time() < deadline:
            time.sleep(2)
    except Exception:
        pass
    stragglers = running() & set(names)
    for n in stragglers:
        # kill-ok (reviewed 8/01): fallback for a holder that ignored the
        # stop-file; the heal below clears the wedge this can cause
        subprocess.run(PS + [
            f"Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            f"Where-Object {{$_.CommandLine -match '{n}'}} | ForEach-Object "
            f"{{ Stop-Process -Id $_.ProcessId -Force -Confirm:$false }}"],
            capture_output=True)   # kill-ok: reviewed fallback (see above)
    if stragglers:
        subprocess.run(PS + ["Restart-Service -Name SDRplayAPIService -Force "
                             "-Confirm:$false"], capture_output=True,
                       timeout=120)  # pipe-ok: Restart-Service emits nothing
        time.sleep(12)
    else:
        time.sleep(3)   # let the released device settle before the TV run


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
