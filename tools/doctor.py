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
                fail("SoapySDR only sees audio devices (no SDR)",
                     "three usual causes: (1) another program is USING the "
                     "SDR right now - busy radios vanish from the list, close "
                     "other SDR apps; (2) vendor driver missing - Windows: "
                     "install the SDRplay API v3 / run from a radioconda "
                     "prompt, Linux: docs/install/linux.md 'SDRplay on "
                     "Linux'; (3) it's not plugged in (short direct USB)")
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
             "python3-soapysdr soapysdr-module-all")

    # 3. external tools
    for tool, why, fix in (
            ("ffmpeg", "recording/remux", "Windows: extract a full build to "
             "C:\\ffmpeg and add C:\\ffmpeg\\bin to PATH. Linux: apt install ffmpeg"),
            ("mpv", "video playback", "Windows: mpv.io or the default player "
             "path in the tools. Linux: apt install mpv")):
        if shutil.which(tool):
            ok(f"{tool} on PATH ({why})")
        else:
            # some setups hardcode known install paths - check the usual ones
            hard = {"ffmpeg": [r"C:\ffmpeg\bin\ffmpeg.exe"],
                    "mpv": [r"C:\Program Files\MPV Player\mpv.exe"]}
            if os.name == "nt" and any(Path(p).exists() for p in hard[tool]):
                ok(f"{tool} found at its default install path ({why})")
            else:
                fail(f"{tool} not found ({why})", fix)

    # 4. platform notes
    if sys.platform.startswith("linux"):
        _linux_usb_checks()
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
