"""cc_gauntlet.py — all-channel stress test on the directional antenna.
Per channel: pick the best of three gain cells by MER, then run 70 s with
the cliff-edge recovery config and report headers/s, delivery gaps, and
WHAT THE CAPTIONS SAY (the content-layer proof of decode)."""
import json, math, os, re, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, r"Z:\src\adaptive-tv")
from tv_lab import ts_metrics, kill_chain, LIVE, PY

TV_LIVE = r"Z:\src\magic-tv-decoder\tools\tv_live.py"
LAB = Path(r"Z:\src\adaptive-tv\lab")
FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe"
RE_FS = re.compile(r"fs_err_rms=([\d.]+)")
CELLS = [(4, 40), (2, 36), (3, 40)]
CHANNELS = [15, 21, 31, 34, 36]

def env_for(rfsel, ifgr):
    e = os.environ.copy()
    e["PATH"] = r"C:\Program Files\SDRplay\API\x64;C:\ffmpeg\bin;" + e.get("PATH", "")
    e.update({"STVT_ANTENNA": "Antenna A", "STVT_IFGR": str(ifgr),
              "STVT_RFGAIN_SEL": str(rfsel), "STVT_EQ": "long",
              "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1", "STVT_DABNOTCH": "1",
              "STVT_RS": "erasure", "STVT_RS_ERASURES": "20",
              "STVT_EQ_QUALITY_BAD_RMS": "8", "STVT_SPS": "1.1",
              "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1", "STVT_EQ_LKG": "1",
              "STVT_EQ_LKG_RMS": "1.0", "STVT_EQ_TELEM": "1"})
    return e

def run_chain(rf, rfsel, ifgr, secs, logfile=None):
    if LIVE.exists():
        try: LIVE.unlink()
        except OSError: pass
    out = open(logfile, "w") if logfile else subprocess.DEVNULL
    ch = subprocess.Popen([PY, "-u", TV_LIVE, "--rf", str(rf)],
                          env=env_for(rfsel, ifgr), stdout=out,
                          stderr=subprocess.STDOUT if logfile else subprocess.DEVNULL)
    time.sleep(secs)
    ch.terminate()
    try: ch.wait(timeout=6)
    except Exception: ch.kill()

def quick_mer(rf, rfsel, ifgr):
    logf = LAB / "gauntlet_cell.log"
    run_chain(rf, rfsel, ifgr, 13, str(logf))
    errs = [float(m.group(1)) for m in RE_FS.finditer(logf.read_text(errors="ignore"))]
    tail = errs[len(errs)//3:]
    return (sum(20*math.log10(5.0/e) for e in tail if e > 0)/len(tail)) if tail else 0.0

def cc_read():
    try:
        snap = LAB / "gauntlet_cc.ts"
        data = LIVE.read_bytes()
        snap.write_bytes(data[-min(len(data), 40*1024*1024):])
        p = subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi",
                            "-i", f"movie={snap.name}[out0+subcc]",
                            "-map", "0:s:0", "-f", "srt", "-"],
                           cwd=str(LAB), capture_output=True, text=True, timeout=90)
        text = re.sub(r"\d+\n[\d:,>\- ]+\n|<[^>]+>|\{\\an\d\}|\\h", "", p.stdout)
        words = re.findall(r"[A-Za-z']{2,}", text)
        letters = sum(len(w) for w in words)
        chars = len(re.sub(r"\s", "", text))
        sample = " ".join(text.split())[:110]
        return chars, (round(letters/chars, 2) if chars else 0.0), sample
    except Exception as e:
        return 0, 0.0, f"(cc error: {e})"

print(f"CC GAUNTLET — directional antenna, {len(CHANNELS)} channels", flush=True)
for rf in CHANNELS:
    kill_chain(); time.sleep(1)
    best, best_mer = CELLS[0], -1
    for cell in CELLS:
        mer = quick_mer(rf, *cell)
        if mer > best_mer: best, best_mer = cell, mer
    if best_mer < 5:
        print(f"RF{rf}: no lock (best MER {best_mer:.1f}) — skipped", flush=True)
        continue
    print(f"RF{rf}: best cell {best[0]}:{best[1]} MER {best_mer:.2f} — "
          f"running 70s stress...", flush=True)
    run_chain(rf, *best, 70)
    m = ts_metrics(45)
    chars, ratio, sample = cc_read()
    if m:
        print(f"  hdrs/s={m['hdrs_s']:.1f} gaps/min={m['gaps_min']:.0f} "
              f"real={m['real_pct']:.0f}%", flush=True)
    print(f"  CC: {chars} chars (wordness {ratio})", flush=True)
    if sample:
        print(f"  THE TV SAID: \"{sample}\"", flush=True)
kill_chain()
print("GAUNTLET DONE", flush=True)
