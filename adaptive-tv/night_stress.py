#!/usr/bin/env python3
"""night_stress.py - all-channel overnight stress test of the main-universal build.

Per locked channel from the antenna scans (best antenna each): quick 2-cell gain
probe -> 100 s live dwell -> measure
  * delivery: MPEG seq-headers/s + TS size (the decode-volume rail)
  * cleanliness: ffmpeg null-sink error lines (the honest quality rail)
  * CAPTIONS: CEA-608 extract - chars, wordness, and what the TV actually said
    (content-layer proof the 1af8a6c parity fix still decodes captions)
  * AUDIO census: every audio stream + language tags - the Spanish/SAP check
Writes lab/night_stress.jsonl + a human table to stdout. Compare channels vs
their scan expectations (lock + program count) for the regression verdict.
"""
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
PY = os.path.join(os.environ["USERPROFILE"], "radioconda", "python.exe")
TV_LIVE = r"Z:\src\magic-tv-decoder\tools\tv_live.py"
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
LAB = Path(r"Z:\src\adaptive-tv\lab")
FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe"
FFPROBE = r"C:\ffmpeg\bin\ffprobe.exe"
RE_FS = re.compile(r"fs_err_rms=([\d.]+)")

# best antenna per locked RF, distilled from lab/scans/* (physical->port mapping:
# Old Faithful=Antenna B, rabbit ears=Antenna A, roof discone=Antenna C)
PLAN = [  # (rf, port, callsign, expected_programs, gain cells (rfsel,ifgr))
    (7,  "Antenna C", "WJLA", 6, [(5, 32), (5, 40)]),      # recipe 7/21: 5/32
    (9,  "Antenna B", "WUSA", 7, [(3, 36), (4, 40)]),
    (15, "Antenna B", "WFDC", 7, [(3, 36), (4, 40)]),      # Univision - Spanish!
    (21, "Antenna B", "WDCW", 5, [(3, 36), (4, 40)]),
    (23, "Antenna A", "",     8, [(3, 40), (4, 40)]),
    (24, "Antenna A", "",     6, [(3, 40), (4, 40)]),
    (28, "Antenna A", "",     6, [(3, 40), (4, 40)]),
    (31, "Antenna B", "WPXW", 5, [(3, 36), (4, 40)]),
    (34, "Antenna B", "WRC",  6, [(3, 36), (4, 40)]),
    (35, "Antenna B", "WDCA", 8, [(3, 36), (4, 40)]),
    (36, "Antenna B", "WTTG", 7, [(3, 36), (4, 40)]),
]
DWELL_S = 100
PROBE_S = 13


def env_for(antenna, rfsel, ifgr):
    e = os.environ.copy()
    e["PATH"] = r"C:\Program Files\SDRplay\API\x64;C:\ffmpeg\bin;" + e.get("PATH", "")
    e.update({"STVT_ANTENNA": antenna, "STVT_IFGR": str(ifgr),
              "STVT_RFGAIN_SEL": str(rfsel), "STVT_EQ": "long",
              "STVT_VITERBI": "soft", "STVT_RS": "erasure", "STVT_SOVA": "1",
              "STVT_TURBO": "1", "STVT_TEISCRUB": "1", "STVT_EQ_LKG": "1",
              "STVT_FPLL_FOLD": "1", "STVT_EQ_TELEM": "1"})
    return e


def kill_chain():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    "Where-Object { $_.CommandLine -match 'tv_live' } | "
                    # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign)
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],  # kill-ok (see above)
                   capture_output=True)


def run_chain(rf, antenna, rfsel, ifgr, secs, logfile):
    if LIVE.exists():
        try:
            LIVE.unlink()
        except OSError:
            pass
    with open(logfile, "w") as out:
        ch = subprocess.Popen([PY, "-u", TV_LIVE, "--rf", str(rf)],
                              env=env_for(antenna, rfsel, ifgr),
                              stdout=out, stderr=subprocess.STDOUT)
        time.sleep(secs)
        ch.terminate()
        try:
            ch.wait(timeout=6)
        except Exception:
            ch.kill()


def quick_mer(rf, antenna, cell):
    logf = LAB / "ns_probe.log"
    run_chain(rf, antenna, cell[0], cell[1], PROBE_S, str(logf))
    errs = [float(m.group(1)) for m in RE_FS.finditer(logf.read_text(errors="ignore"))]
    tail = errs[len(errs) // 3:]
    return (sum(20 * math.log10(5.0 / e) for e in tail if e > 0) / len(tail)) if tail else 0.0


def snapshot():
    snap = LAB / "ns_snap.ts"
    data = LIVE.read_bytes() if LIVE.exists() else b""
    snap.write_bytes(data[-min(len(data), 60 * 1024 * 1024):])
    return snap, len(data)


def analyze(snap):
    r = {}
    ts = snap.read_bytes()
    r["headers"] = ts.count(b"\x00\x00\x01\xb3")
    p = subprocess.run([FFMPEG, "-v", "error", "-i", str(snap), "-map", "0:v?",
                        "-f", "null", "-"], capture_output=True, text=True, timeout=180)
    r["err_lines"] = len([ln for ln in p.stderr.splitlines() if ln.strip()])
    # audio census (the Spanish/SAP check)
    p = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-of", "json",
                        str(snap)], capture_output=True, text=True, timeout=120)
    auds = []
    try:
        for s in json.loads(p.stdout or "{}").get("streams", []):
            if s.get("codec_type") == "audio":
                auds.append({"codec": s.get("codec_name"),
                             "lang": (s.get("tags") or {}).get("language", "?"),
                             "ch": s.get("channels")})
    except Exception:
        pass
    r["audio"] = auds
    r["spanish"] = any(a.get("lang") in ("spa", "esp") for a in auds)
    # captions: what did the TV say?
    try:
        p = subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi",
                            "-i", f"movie={snap.name}[out0+subcc]",
                            "-map", "0:s:0", "-f", "srt", "-"],
                           cwd=str(LAB), capture_output=True, text=True, timeout=120)
        text = re.sub(r"\d+\n[\d:,>\- ]+\n|<[^>]+>|\{\\an\d\}|\\h", "", p.stdout)
        words = re.findall(r"[A-Za-z']{2,}", text)
        letters = sum(len(w) for w in words)
        chars = len(re.sub(r"\s", "", text))
        r["cc_chars"] = chars
        r["cc_wordness"] = round(letters / chars, 2) if chars else 0.0
        r["cc_sample"] = " ".join(text.split())[:110]
    except Exception as e:
        r["cc_chars"], r["cc_wordness"], r["cc_sample"] = 0, 0.0, f"(cc err {e})"
    return r


def main():
    out_path = LAB / "night_stress.jsonl"
    print(f"NIGHT STRESS - {len(PLAN)} channels, {DWELL_S}s dwell each "
          f"(main-universal build, erasure+SOVA+turbo live env)", flush=True)
    results = []
    with open(out_path, "a", encoding="utf-8") as fout:
        for rf, ant, cs, exp_progs, cells in PLAN:
            kill_chain(); time.sleep(1.5)
            mers = [(quick_mer(rf, ant, c), c) for c in cells]
            best_mer, best = max(mers)
            if best_mer < 5:
                rec = {"rf": rf, "cs": cs, "verdict": "NO LOCK",
                       "best_mer": round(best_mer, 1)}
                print(f"RF{rf:2d} {cs:5s}: NO LOCK (best MER {best_mer:.1f}) - "
                      f"expected lock in scan -> REGRESSION CANDIDATE (or band/antenna moved)",
                      flush=True)
                results.append(rec); fout.write(json.dumps(rec) + "\n"); fout.flush()
                continue
            print(f"RF{rf:2d} {cs:5s}: MER {best_mer:.1f} @ {ant} cell {best} - "
                  f"{DWELL_S}s dwell...", flush=True)
            run_chain(rf, ant, best[0], best[1], DWELL_S, str(LAB / f"ns_rf{rf}.log"))
            snap, total = snapshot()
            a = analyze(snap)
            rec = {"rf": rf, "cs": cs, "ant": ant, "cell": best,
                   "mer": round(best_mer, 1), "ts_bytes": total, **a,
                   "exp_progs": exp_progs, "ts": time.strftime("%H:%M:%S")}
            results.append(rec); fout.write(json.dumps(rec) + "\n"); fout.flush()
            langs = ",".join(sorted({x['lang'] for x in a['audio']})) or "-"
            print(f"    hdrs={a['headers']} err={a['err_lines']} "
                  f"audio_streams={len(a['audio'])} langs=[{langs}] "
                  f"spanish={'YES' if a['spanish'] else 'no'} cc={a['cc_chars']}ch "
                  f"wordness={a['cc_wordness']}", flush=True)
            if a.get("cc_sample"):
                print(f"    THE TV SAID: \"{a['cc_sample']}\"", flush=True)
    kill_chain()
    # verdict table
    print("\n==== NIGHT STRESS VERDICT ====", flush=True)
    locked = [r for r in results if r.get("verdict") != "NO LOCK"]
    cc_ok = [r for r in locked if r.get("cc_chars", 0) > 200 and r.get("cc_wordness", 0) >= 0.6]
    spa = [r for r in locked if r.get("spanish")]
    print(f"locked {len(locked)}/{len(results)} channels | captions proven on "
          f"{len(cc_ok)} | Spanish audio found on {len(spa)}: "
          f"{[('RF%d %s' % (r['rf'], r['cs'])) for r in spa]}", flush=True)
    print("done -> lab/night_stress.jsonl", flush=True)


if __name__ == "__main__":
    main()
