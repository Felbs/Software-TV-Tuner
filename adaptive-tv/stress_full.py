#!/usr/bin/env python3
"""stress_full.py - drive EVERY locked channel and prove the whole stack.

Per channel: start the real chain, measure LOCK TIME (until video actually
decodes), soak, score DECODE health (ffmpeg null-sink fps + error rate), and
read the CAPTIONS (the content-layer proof). Prints a table. Leaves the best
channel's chain running so a player can show visible TV.

  python stress_full.py            # all channels from the last scan
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = r"C:\Users\user\radioconda\python.exe"
TV_LIVE = r"Z:\src\magic-tv-decoder\tools\tv_live.py"
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
LAB = HERE / "lab"
SCAN = Path(os.path.expanduser("~")) / ".tv_tuner" / "scan.json"
FFMPEG = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
ANT = "Antenna B"
GAINS = {36: (3, 40), 34: (2, 32), 15: (1, 32), 7: (5, 32), 9: (5, 32),
         21: (2, 32), 31: (2, 32), 35: (2, 32)}
DEFAULT_GAIN = (3, 40)
RE_FS = re.compile(r"fs_err(?:_rms)?[=: ]+([\d.]+)")
LOCK_TIMEOUT = 55.0
SOAK = 22.0


def env_for(rf):
    rfsel, ifgr = GAINS.get(rf, DEFAULT_GAIN)
    e = os.environ.copy()
    e["PATH"] = (r"C:\Program Files\SDRplay\API\x64;C:\ffmpeg\bin;"
                 + r"C:\Users\user\radioconda\Library\bin;" + e.get("PATH", ""))
    e.update({"STVT_ANTENNA": ANT, "STVT_RFGAIN_SEL": str(rfsel), "STVT_IFGR": str(ifgr),
              "STVT_EQ": "long", "STVT_RS": "stock", "STVT_VITERBI": "soft",
              "STVT_DABNOTCH": "0", "STVT_EQ_TELEM": "1"})
    return e


def _decodes(nbytes=6_000_000):
    """(unique_pid_count, decoded_frames) from the live.ts tail. PID count is the
    ROBUST lock signal (a healthy locked ATSC channel emits ~20-40 PIDs; noise/no
    lock emits 0; a drought emits hundreds). frames = decode health via ffmpeg at
    the DEFAULT log level (so the 'frame=' progress line actually prints - the
    -v error bug hid it)."""
    try:
        data = LIVE.read_bytes()
    except Exception:
        return 0, 0
    tail = data[-min(len(data), nbytes):]
    s = set(); i = tail.find(b"\x47")
    while i >= 0 and i + 188 <= len(tail):
        if tail[i] == 0x47:
            s.add(((tail[i + 1] & 0x1f) << 8) | tail[i + 2]); i += 188
        else:
            i += 1
    pids = len(s)
    frames = 0
    try:
        snap = LAB / "_stress_snap.ts"; snap.write_bytes(tail)
        p = subprocess.run([FFMPEG, "-i", str(snap), "-map", "0:v:0?", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=40)
        ms = re.findall(r"frame=\s*(\d+)", p.stderr)
        frames = int(ms[-1]) if ms else 0
    except Exception:
        pass
    return pids, frames


def cc_read():
    """Extract EIA-608 captions from live.ts tail -> (chars, sample text)."""
    try:
        snap = LAB / "_stress_cc.ts"
        data = LIVE.read_bytes()
        snap.write_bytes(data[-min(len(data), 40 * 1024 * 1024):])
        p = subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i",
                            f"movie={snap.name}[out0+subcc]", "-map", "0:s:0",
                            "-f", "srt", "-"], cwd=str(LAB), capture_output=True,
                           text=True, timeout=90)
        text = re.sub(r"\d+\n[\d:,>\- ]+\n|<[^>]+>", "", p.stdout)
        chars = len(re.sub(r"\s", "", text))
        return chars, " ".join(text.split())[:80]
    except Exception:
        return 0, ""


def run_one(rf, callsign):
    if LIVE.exists():
        try:
            LIVE.unlink()
        except OSError:
            pass
    log = LAB / f"_stress_{rf}.log"
    fh = open(log, "w")
    t0 = time.time()
    ch = subprocess.Popen([PY, "-u", TV_LIVE, "--rf", str(rf)], env=env_for(rf),
                          stdout=fh, stderr=subprocess.STDOUT)
    lock_t = None
    # LOCK = the TS carries a healthy PID set (real decoded channel)
    while time.time() - t0 < LOCK_TIMEOUT:
        time.sleep(2.0)
        if LIVE.exists() and LIVE.stat().st_size > 2_500_000:
            pids, _ = _decodes()
            if 8 <= pids <= 80:
                lock_t = round(time.time() - t0, 1)
                break
    result = {"rf": rf, "call": callsign, "lock_s": lock_t}
    if lock_t is None:
        ch.terminate()
        try:
            ch.wait(timeout=6)
        except Exception:
            ch.kill()
        fh.close()
        import math
        good = [float(m.group(1)) for m in RE_FS.finditer(log.read_text(errors="ignore"))]
        good = [e for e in good if e > 0]
        mer = round(sum(20 * math.log10(5.0 / e) for e in good[-10:]) / len(good[-10:]), 1) if good else 0
        result.update({"lock_s": None, "pids": 0, "frames": 0, "cc_chars": 0, "cc": "",
                       "verdict": "NO LOCK", "mer": mer})
        return result, ch
    time.sleep(SOAK)                       # soak, accumulate TS for decode+CC scoring
    pids, frames = _decodes(6_000_000)
    cc_chars, cc_sample = cc_read()
    errs = [float(m.group(1)) for m in RE_FS.finditer(log.read_text(errors="ignore"))]
    tail = errs[len(errs) // 3:] if errs else []
    import math
    good = [e for e in tail if e > 0]
    mer = round(sum(20 * math.log10(5.0 / e) for e in good) / len(good), 1) if good else 0
    verdict = ("CLEAN" if pids >= 12 and cc_chars > 0 else
               "PLAYS" if pids >= 12 and mer >= 15 else
               "MARGINAL" if pids >= 8 else "WEAK")
    result.update({"pids": pids, "frames": frames, "mer": mer, "cc_chars": cc_chars,
                   "cc": cc_sample, "verdict": verdict})
    return result, ch


def main():
    d = json.loads(SCAN.read_text(encoding="utf-8"))
    locked = [(c["rf"], c.get("virtual") or c.get("callsign") or "?")
              for c in d.get("channels", []) if c.get("lock")]
    print(f"=== STRESS: {len(locked)} locked channels on {d.get('antenna')} ===", flush=True)
    print(f"{'RF':>3} {'station':8} {'lock':>6} {'PIDs':>5} {'frames':>7} {'MER':>5} {'CC':>5}  captions", flush=True)
    print("-" * 82, flush=True)
    rows = []
    best = (None, -1)
    for rf, call in locked:
        r, ch = run_one(rf, call)
        rows.append(r)
        lock = f"{r['lock_s']}s" if r.get("lock_s") else "NONE"
        print(f"{rf:>3} {str(call)[:8]:8} {lock:>6} {r.get('pids',0):>5} {r.get('frames',0):>7} "
              f"{r.get('mer', 0):>5} {r.get('cc_chars',0):>5}  "
              f"{(r.get('cc') or r.get('verdict',''))[:42]}", flush=True)
        # keep the best-decoding chain candidate; kill the rest
        score = r.get("pids", 0) * (3 if r.get("cc_chars", 0) > 0 else 1)
        if score > best[1]:
            if best[0] is not None:
                try:
                    best[0].terminate()
                except Exception:
                    pass
            best = (ch, score, rf)
        else:
            try:
                ch.terminate()
            except Exception:
                pass
    print("-" * 78, flush=True)
    ok = [r for r in rows if r.get("verdict") in ("CLEAN", "PLAYS")]
    cc = [r for r in rows if r.get("cc_chars", 0) > 0]
    locks = [r["lock_s"] for r in rows if r.get("lock_s")]
    print(f"SUMMARY: {len(ok)}/{len(rows)} channels play, {len(cc)} with captions, "
          f"lock times {min(locks) if locks else '-'}-{max(locks) if locks else '-'}s "
          f"(median {sorted(locks)[len(locks)//2] if locks else '-'}s)", flush=True)
    if len(best) > 2:
        print(f"BEST channel RF{best[2]} left running for visible playback.", flush=True)
    json.dump(rows, open(LAB / "stress_full_result.json", "w"), indent=2)


if __name__ == "__main__":
    main()
