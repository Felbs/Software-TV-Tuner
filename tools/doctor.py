"""doctor.py - is this machine ready to decode TV? One command, every
dependency checked, and the exact fix printed for anything missing.

    python tools/doctor.py

Works on Windows (radioconda), Linux, WSL, and Raspberry Pi. If you're
asking for help, paste this output - it answers the first ten questions.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

GOOD = "  [ OK ] "
BAD = "  [FAIL] "
WARN = "  [warn] "
results = {"ok": 0, "fail": 0}


def ok(msg):
    results["ok"] += 1
    print(GOOD + msg)


def fail(msg, fix):
    results["fail"] += 1
    print(BAD + msg)
    print("         fix: " + fix)


def warn(msg, note=""):
    print(WARN + msg + (("  (" + note + ")") if note else ""))


def _ensure_sdr_dll_path():
    """Windows: put the SoapySDR driver DLL dirs on PATH so device
    enumeration works even outside an activated radioconda prompt."""
    if os.name != "nt":
        return
    root = Path(sys.executable).resolve().parent
    for p in (root / "Library" / "bin",
              Path(r"C:\Program Files\SDRplay\API\x64"),
              Path(r"C:\Program Files\SDRplay\API")):
        if p.is_dir():
            os.environ["PATH"] = str(p) + os.pathsep + os.environ["PATH"]
            try:
                os.add_dll_directory(str(p))
            except Exception:
                pass


def _windows_autoheal():
    """Windows AUTO-HEAL for the two classic SDRplay landmines (they cost this
    project's own rig hours; strangers hit them with no memory bank):
    1. SDRplay API dir falls OFF the User PATH after any SDRplay/SDRuno
       reinstall -> everything silently sees zero radios. We PERSIST the fix.
    2. Wedged USB controller: Windows Device Manager still shows the radio but
       the driver can't open it -> tell the user the real cure in plain words.
    Returns True if the wedge was diagnosed (so callers can skip generic advice)."""
    if os.name != "nt":
        return False
    api = Path(r"C:\Program Files\SDRplay\API\x64")
    if api.is_dir():
        try:
            q = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "[Environment]::GetEnvironmentVariable('PATH','User')"],
                capture_output=True, text=True, timeout=15)
            user_path = q.stdout.strip()
            if str(api).lower() not in user_path.lower():
                newp = (user_path.rstrip(";") + ";" if user_path else "") + str(api)
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"[Environment]::SetEnvironmentVariable('PATH','{newp}','User')"],
                    capture_output=True, timeout=15)
                ok("HEALED: SDRplay API dir was missing from your User PATH - "
                   "added it permanently (this recurs after every SDRplay/SDRuno "
                   "reinstall; new terminals will now just work)")
        except Exception:
            pass
    # wedged-USB: PnP sees an RSP but SoapySDR can't - a controller state only
    # a replug/reboot clears (documented RSPdx behaviour, not our bug)
    try:
        q = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-PnpDevice | Where-Object { $_.FriendlyName -match 'RSP|SDRplay' "
             "-and $_.Status -eq 'OK' } | Measure-Object).Count"],
            capture_output=True, text=True, timeout=20)
        if int(q.stdout.strip() or 0) > 0:
            fail("USB controller is WEDGED: Windows still lists your SDRplay "
                 "radio, but its driver can no longer open it",
                 "(0) FIRST close any program that might be using the radio - "
                 "busy radios also vanish from the list; then if still gone: "
                 "(1) unplug the radio's USB cable, wait 5 s, replug; "
                 "(2) if it's still invisible, REBOOT the PC - that always "
                 "clears it (known RSP/USB3 state, not a software bug). "
                 "Use a short, direct USB 3.0 cable (no hubs)")
            return True
    except Exception:
        pass
    return False


def _linux_usb_checks():
    """Linux: the USB plumbing Windows' vendor driver handles for you.
    An autosuspending SDR drops samples / vanishes mid-stream; a dead
    sdrplay API service makes the radio invisible; a tiny usbfs cap
    starves 8 MS/s streams."""
    usb_root = Path("/sys/bus/usb/devices")
    if not usb_root.is_dir():
        return  # container / WSL without USB — nothing to check

    # SDRplay radios present? (vendor 1df7)
    sdrplay_devs = []
    for d in usb_root.iterdir():
        try:
            if (d / "idVendor").read_text().strip() == "1df7":
                sdrplay_devs.append(d)
        except OSError:
            continue

    for d in sdrplay_devs:
        try:
            ctrl = (d / "power" / "control").read_text().strip()
        except OSError:
            continue
        if ctrl == "on":
            ok(f"SDRplay USB autosuspend disabled ({d.name})")
        else:
            fail(f"SDRplay USB autosuspend is ACTIVE ({d.name}) - the radio "
                 "can be powered down mid-stream",
                 "re-run ./bootstrap.sh (installs a udev rule), or now: "
                 f"echo on | sudo tee /sys/bus/usb/devices/{d.name}"
                 "/power/control")

    # vendor API installed but service not running?
    api_installed = Path("/usr/local/lib/libsdrplay_api.so").exists()
    if api_installed or sdrplay_devs:
        try:
            svc = subprocess.run(["pgrep", "-x", "sdrplay_apiServ"],
                                 capture_output=True, timeout=5)
            running = svc.returncode == 0
        except Exception:
            running = False
        if running:
            ok("SDRplay API service running")
        elif api_installed:
            fail("SDRplay API service NOT running (radio will be invisible)",
                 "sudo systemctl enable --now sdrplay   (then replug the SDR)")
        else:
            warn("SDRplay radio plugged in but vendor API not installed",
                 "docs/install/linux.md 'SDRplay on Linux'")

    # usbfs URB buffer cap
    try:
        cap = int(Path("/sys/module/usbcore/parameters/usbfs_memory_mb")
                  .read_text().strip())
        if 0 < cap < 200:
            warn(f"kernel usbfs buffer cap is {cap} MB (default 16 starves "
                 "high-rate SDRs)",
                 "re-run ./bootstrap.sh, or: echo 1000 | sudo tee "
                 "/sys/module/usbcore/parameters/usbfs_memory_mb")
        else:
            ok(f"usbfs buffer cap {cap if cap else 'unlimited'} MB")
    except (OSError, ValueError):
        pass


def _sdr_is_busy_with_us():
    """True if OUR decode chain currently holds the radio.

    A radio in use disappears from enumeration. Reporting that as "no SDR"
    sends a newcomer hunting for a hardware fault while their television is
    playing perfectly on the next screen.
    """
    try:
        r = subprocess.run(["pgrep", "-af", "tv_" + "live.py"],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def _single_tenant_checks():
    """The three things that make a working Pi look broken to a newcomer.

    Every one of these was found the hard way on 2026-07-30/31: an overnight
    run died after 1h13m because the recovery path could not run, and a
    channel that read as stone dead was one gain setting away. None of them
    announce themselves - the symptom is always "it just stops working".
    """
    if sys.platform == "win32":
        return

    # ---- 1. can the documented SDR cure actually run? -------------------
    # tv_live's self-heal restarts the vendor service when the API wedges.
    # On Linux that needs passwordless sudo for exactly that command. Without
    # it the cure raises, the supervisor burns its retries, and you get the
    # 30-restart collapse with no explanation.
    try:
        # -l asks "may I run this?" and runs nothing. Testing the EXACT
        # command matters: a properly narrow sudoers rule grants only the
        # restart, so probing with is-active would wrongly report failure.
        r = subprocess.run(["sudo", "-n", "-l", "/bin/systemctl", "restart",
                            "sdrplay"], capture_output=True, text=True,
                           timeout=15)
        if r.returncode == 0:
            ok("passwordless sudo for the sdrplay service (self-heal can run)")
        else:
            raise OSError(r.stderr.strip()[:60] or "sudo refused")
    except Exception as e:
        fail("self-heal cannot restart the SDR service without a password "
             f"({type(e).__name__})",
             "add a sudoers rule so recovery works unattended:\n"
             "         echo \"$USER ALL=(root) NOPASSWD: /bin/systemctl restart "
             "sdrplay, /bin/systemctl is-active sdrplay\" | \\\n"
             "           sudo tee /etc/sudoers.d/stvt-sdrplay && sudo chmod 440 "
             "/etc/sudoers.d/stvt-sdrplay\n"
             "         Without this the documented cure for a wedged SDR fails "
             "on Linux and a stalled chain cannot recover on its own.")

    # ---- 2. is anything else going to grab the radio? -------------------
    # ONE SDR, ONE CONSUMER. The panel (~44% CPU) and the network tuner are
    # both perfectly good software that must not run while a direct chain
    # does. "stopped" is not enough - "enabled" means they come back at boot.
    rivals = [("stvt-panel.service", "--user"), ("stvt-hdhr.service", "--user"),
              ("soapyremote-server.service", "--system")]
    armed = []
    for unit, scope in rivals:
        try:
            args = ["systemctl"] + (["--user"] if scope == "--user" else [])
            en = subprocess.run(args + ["is-enabled", unit],
                                capture_output=True, text=True, timeout=10)
            act = subprocess.run(args + ["is-active", unit],
                                 capture_output=True, text=True, timeout=10)
            state, running = en.stdout.strip(), act.stdout.strip()
            if running == "active":
                armed.append(f"{unit} RUNNING NOW")
            elif state == "enabled":
                armed.append(f"{unit} enabled (starts at boot)")
        except Exception:
            continue
    if not armed:
        ok("no rival SDR consumer is running or enabled at boot")
    else:
        fail("another process will fight for the single-tenant SDR: "
             + "; ".join(armed),
             "this Pi runs ONE radio consumer at a time. Pick a mode:\n"
             "         TV on this screen ->  systemctl --user disable --now "
             "stvt-panel stvt-hdhr\n"
             "         network tuner     ->  use stvt-hdhr, and do not launch a "
             "direct chain\n"
             "         Measured cost of ignoring this: 1416 kB/s and 597 CC "
             "errors with the panel up,\n"
             "         2384 kB/s and 19 CC errors with it stopped - same signal, "
             "same code.")

    # ---- 3. tap cache hygiene ------------------------------------------
    # Warm start is a real win, but the banking gate only looks at the error
    # residual, and a channel that never locks can sit under that gate. A
    # poisoned entry teaches the equalizer nonsense on the next tune.
    #
    # Measured 2026-07-31: the tap vector CANNOT tell you which is which.
    # Known-bad taps_AntennaA_rf36 (AM loop at UHF, deaf) reads |taps|=1.531;
    # known-good taps_AntennaB_rf36 reads 1.521. Peak/norm overlaps too. So
    # this check reasons about what each antenna can physically hear instead
    # of pretending the file knows.
    cache = Path(__file__).resolve().parent / "data" / "tv_live" / "tapcache"
    if not cache.is_dir():
        return
    entries = sorted(cache.glob("taps_*_rf*.bin"))
    if not entries:
        return

    def _band(rf):
        return "UHF" if 14 <= rf <= 36 else "VHF"

    # what each port has actually been measured to receive here
    DEAF = {("AntennaA", "UHF"): "the AM loop is deaf at UHF",
            ("AntennaB", "VHF"): "the TV yagi is deaf at VHF",
            ("AntennaC", "UHF"): "port C does not reach UHF"}
    suspect = []
    for f in entries:
        try:
            stem = f.stem.split("_")
            ant, rf = stem[1], int(stem[2].replace("rf", ""))
        except (IndexError, ValueError):
            continue
        why = DEAF.get((ant, _band(rf)))
        if why:
            suspect.append(f"{f.name} ({why})")
    if not suspect:
        ok(f"tap cache: {len(entries)} warm-start entries, none from a "
           f"combination this rig cannot hear")
    else:
        warn(f"tap cache has {len(suspect)} entry(s) banked from a channel "
             f"that cannot lock on that port",
             "; ".join(suspect))
        print("         these were saved by sessions that never decoded, so "
              "they warm-start the")
        print("         equalizer with noise. Delete them:  rm " +
              " ".join(str(cache / s.split(' ')[0]) for s in suspect[:3]) +
              (" ..." if len(suspect) > 3 else ""))
        print("         Real fix (not yet done): stamp the banking fs_err_rms "
              "into the cache file, so")
        print("         a future doctor can judge quality instead of inferring "
              "it from the antenna.")

    # ---- 4. thermal / real-time headroom --------------------------------
    try:
        r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                           text=True, timeout=10)
        val = r.stdout.strip().split("=")[-1]
        if r.returncode == 0 and val.startswith("0x"):
            bits = int(val, 16)
            if bits == 0:
                ok("no thermal throttling or under-voltage recorded")
            else:
                notes = []
                if bits & 0x1:
                    notes.append("under-voltage NOW")
                if bits & 0x4:
                    notes.append("throttled NOW")
                if bits & 0x50000:
                    notes.append("throttled/under-volted since boot")
                warn(f"power or thermal event flagged ({val})",
                     ", ".join(notes) or "see vcgencmd get_throttled")
                print("         a throttled Pi drops samples and looks like bad "
                      "reception. Check the PSU first.")
    except Exception:
        pass


def main():
    print("=" * 62)
    print("Software TV Tuner - install doctor")
    print("=" * 62)
    print(f"python {sys.version.split()[0]}  ({sys.executable})")
    print(f"platform: {sys.platform}" + ("  (WSL)" if "WSL" in os.environ.get(
        "WSL_DISTRO_NAME", "") or Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists()
        and os.name != "nt" else ""))
    print()

    # 1. python packages
    try:
        import numpy  # noqa: F401
        ok(f"numpy {numpy.__version__}")
    except ImportError:
        fail("numpy missing", "pip install numpy  (or use radioconda)")

    try:
        from gnuradio import gr  # noqa: F401
        ok("GNU Radio imports")
    except ImportError:
        fail("GNU Radio not importable",
             "Windows: install radioconda and run from its prompt. "
             "Linux: sudo apt install gnuradio  (or run ./bootstrap.sh)")

    try:
        from gnuradio import atscplus  # noqa: F401
        n = len([b for b in dir(atscplus) if b.startswith("atsc_")])
        ok(f"gr-atscplus decoder module ({n} blocks)")
    except ImportError:
        fail("gr-atscplus not importable (the TV decoder itself)",
             "Windows: gr-atscplus\\_build.bat  |  Linux/WSL/Pi: ./bootstrap.sh "
             "(if you rebuilt by hand, remember: _rebuild compiles but does NOT "
             "install)")

    # 2. SoapySDR + devices
    _ensure_sdr_dll_path()
    try:
        import SoapySDR
        ok("SoapySDR python bindings")
        try:
            SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
        except Exception:
            pass
        try:
            # enumerate in a subprocess with a hard timeout - the remote
            # module's network discovery can stall for minutes
            code = ("import SoapySDR;"
                    "SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL);"
                    "print('DOCTOR|'+'\\x1e'.join(str(d) for d in "
                    "SoapySDR.Device.enumerate('')))")
            r = subprocess.run([sys.executable, "-c", code],
                               capture_output=True, text=True, timeout=25)
            marked = [l for l in (r.stdout or "").splitlines()
                      if l.startswith("DOCTOR|")]
            labels = [s for s in (marked[0][7:].split("\x1e") if marked else [])
                      if s]
            real = [l for l in labels if "audio" not in str(l).lower()]
            if real:
                ok("SDR found: " + "; ".join(str(r)[:50] for r in real[:3]))
                # which radio will STVT actually open? (issue #2: a
                # perfectly-probing Pluto was never even attempted)
                try:
                    import sdr_compat
                    resolved = sdr_compat.resolve_soapy_args(verbose=False)
                    if sdr_compat.is_sdrplay(resolved):
                        ok(f"STVT will open: {resolved}")
                    else:
                        ok(f"STVT will open: {resolved} (non-SDRplay: "
                           f"generic gain mapping; override with "
                           f"STVT_SOAPY_ARGS)")
                except Exception:
                    pass
                # full-rate link check: gappy USB looks like a bad antenna
                try:
                    import probe_throughput as _ptp
                    lk = _ptp.measure(seconds=2.0)
                    if lk["delivered_pct"] >= 99.5 and lk["overflows"] <= 25:
                        ok(f"USB link sustains full rate "
                           f"({lk['delivered_pct']:.1f}% delivered)")
                    else:
                        fail(f"USB link gaps under load "
                             f"({lk['delivered_pct']:.0f}% delivered, "
                             f"{lk['overflows']} overflows)",
                             _ptp.LINK_FIX_HINT)
                except Exception as e:
                    warn(f"link check skipped ({str(e)[:50]})",
                         "SDR may be in use by another program")
            elif labels:
                if _windows_autoheal():
                    pass                       # wedge diagnosed - skip generic advice
                else:
                    if _sdr_is_busy_with_us():
                        ok("SDR is present but BUSY - our own decode chain "
                           "holds it (correct while TV is playing)")
                    else:
                        fail("SoapySDR only sees audio devices (no SDR)",
                         "three usual causes: (1) another program is USING the "
                         "SDR right now - busy radios vanish from the list, close "
                         "other SDR apps; (2) vendor driver missing - Windows: "
                         "install the SDRplay API v3 / run from a radioconda "
                         "prompt, Linux: docs/install/linux.md 'SDRplay on "
                         "Linux'; (3) it's not plugged in (short direct USB)")
            elif _sdr_is_busy_with_us():
                ok("SDR is present but BUSY - our own decode chain holds it "
                   "(this is correct while TV is playing)")
            else:
                fail("SoapySDR enumerates zero devices",
                     "plug the SDR in (short direct USB), install its vendor "
                     "driver, and on SDRplay restart the API service")
        except subprocess.TimeoutExpired:
            warn("device enumeration timed out after 25 s",
                 "usually the remote/network module scanning - not fatal, "
                 "try SoapySDRUtil --find=driver=<yours>")
        except Exception as e:
            fail(f"device enumeration crashed ({str(e)[:40]})",
             "reinstall SoapySDR / vendor module")
    except ImportError:
        fail("SoapySDR python bindings missing",
             "Windows: radioconda has them. Linux: sudo apt install "
             "python3-soapysdr soapysdr-module-all. Built SoapySDR by "
             "hand into /usr/local (SoapySDRUtil works but python "
             "can't import)? Then: "
             "find /usr/local/lib -name SoapySDR.py  and  "
             "export PYTHONPATH=<that dir>:$PYTHONPATH")

    # 3. external tools
    for tool, why, fix in (
            ("ffmpeg", "recording/remux", "Windows: extract a full build to "
             "C:\\ffmpeg and add C:\\ffmpeg\\bin to PATH. Linux: apt install ffmpeg"),
            ("ffplay", "default live playback", "ships with the FULL ffmpeg "
             "build, NOT the 'essentials' one - re-download the full/GPL build "
             "(Windows: gyan.dev or BtbN) so ffplay.exe sits beside ffmpeg.exe. "
             "Linux: apt install ffmpeg. Or play with mpv instead (--player mpv)."),
            ("mpv", "alternate player (--player mpv)", "Windows: mpv.io. "
             "Linux: apt install mpv. Optional if ffplay is present.")):
        if shutil.which(tool):
            ok(f"{tool} on PATH ({why})")
        else:
            # some setups hardcode known install paths - check the usual ones
            hard = {"ffmpeg": [r"C:\ffmpeg\bin\ffmpeg.exe"],
                    "ffplay": [r"C:\ffmpeg\bin\ffplay.exe"],
                    "mpv": [r"C:\Program Files\MPV Player\mpv.exe"]}
            if os.name == "nt" and any(Path(p).exists() for p in hard[tool]):
                ok(f"{tool} found at its default install path ({why})")
            elif tool == "mpv":
                # mpv is optional when ffplay works - advise, don't fail
                warn(f"{tool} not found ({why})", fix)
            else:
                fail(f"{tool} not found ({why})", fix)

    # 4. platform notes
    if sys.platform.startswith("linux"):
        _linux_usb_checks()
    _single_tenant_checks()
    if os.name == "nt":
        api = Path(r"C:\Program Files\SDRplay\API")
        if api.is_dir():
            ok("SDRplay API installed")
        else:
            warn("SDRplay API not installed", "only needed for SDRplay radios")
    print()
    print("=" * 62)
    if results["fail"] == 0:
        print(f"ALL {results['ok']} CHECKS PASS - go decode some television:")
        print("  python tools/tv_tuner.py")
    else:
        print(f"{results['ok']} ok, {results['fail']} to fix - see the 'fix:' "
              f"lines above, then re-run me.")
    print("=" * 62)
    return 0 if results["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
