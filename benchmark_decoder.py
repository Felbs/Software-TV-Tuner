#!/usr/bin/env python3
"""Benchmark our ATSC decode pipeline against the locked HDHR baseline.

Reads `data/decoder_compare/baseline_v0/iq.cf32` (the canonical 8-sec
RF 34 capture), runs whatever decode pipeline you specify, compares
output to `hdhr_groundtruth.ts`, and writes a scorecard to
`benchmark_results.json` (appended history).

Usage:
    python benchmark_decoder.py --label "v0 baseline (gr-dtv native)"
    python benchmark_decoder.py --label "v1 256-tap eq" --decoder my_long_eq.py

Each entry in the scorecard JSON includes:
  - label
  - timestamp
  - HD frames decoded (vs HDHR's 199 baseline)
  - Total clean packets
  - Per-PID byte overlap with HDHR (if alignment is found)
  - elapsed_sec for the run
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = Path(__file__).parent
BASELINE = ROOT / "data" / "decoder_compare" / "baseline_v0"
IQ_PATH = BASELINE / "iq.cf32"
GT_TS = BASELINE / "hdhr_groundtruth.ts"
RESULTS_FILE = ROOT / "benchmark_results.json"


def cs16_from_cf32(cf32_path: Path, cs16_path: Path):
    """Convert complex64 IQ to interleaved CS16 (what gr-dtv expects)."""
    import numpy as np
    iq = np.fromfile(cf32_path, dtype=np.complex64)
    out = np.empty(iq.size * 2, dtype=np.int16)
    out[0::2] = np.clip(iq.real * 32767, -32768, 32767).astype(np.int16)
    out[1::2] = np.clip(iq.imag * 32767, -32768, 32767).astype(np.int16)
    out.tofile(cs16_path)


def run_grdtv_native(cs16_path: Path, ts_path: Path):
    """Default decode: native-rate gr-dtv pipeline (current best)."""
    cs_wsl = "/mnt/c" + str(cs16_path).replace("C:", "").replace("\\", "/")
    ts_wsl = "/mnt/c" + str(ts_path).replace("C:", "").replace("\\", "/")
    script = f"""
from gnuradio import gr, blocks, dtv
from gnuradio import filter as gr_filter
class TB(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self)
        self.connect(blocks.file_source(gr.sizeof_short, '{cs_wsl}', False),
                     blocks.interleaved_short_to_complex(),
                     gr_filter.rational_resampler_ccc(interpolation=25, decimation=32),
                     dtv.atsc_rx(6_250_000, 1.5),
                     blocks.file_sink(gr.sizeof_char, '{ts_wsl}'))
tb = TB(); tb.start(); tb.wait()
"""
    subprocess.run(["wsl", "python3", "-c", script],
                   stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   timeout=300)


def scrub_tei(ts_in: Path, ts_out: Path) -> dict:
    """Rewrite TEI=1 + bad-PID packets to NULL — preserves continuity."""
    KEEP = {0x0000, 0x1FFB, 0x1FFF}
    for p in range(0x30, 0x90, 0x10):
        for s in range(6):
            KEEP.add(p | s)
    data = bytearray(ts_in.read_bytes())
    n = len(data) // 188
    clean = 0
    for i in range(n):
        b = i * 188
        if data[b] != 0x47:
            data[b] = 0x47; data[b+1] = 0x1F; data[b+2] = 0xFF; data[b+3] = 0x10
            continue
        tei = (data[b+1] >> 7) & 1
        pid = ((data[b+1] & 0x1F) << 8) | data[b+2]
        if tei or pid not in KEEP:
            data[b+1] = 0x1F; data[b+2] = 0xFF
        else:
            clean += 1
    ts_out.write_bytes(data)
    return dict(total=n, clean=clean, clean_frac=clean/max(n, 1))


def count_frames(ts_path: Path, stream_idx: int = 0) -> int:
    """ffmpeg-decode video stream at index, return frame count."""
    ts_wsl = "/mnt/c" + str(ts_path).replace("C:", "").replace("\\", "/")
    try:
        r = subprocess.run(
            ["wsl", "ffmpeg", "-y", "-hide_banner",
             "-err_detect", "ignore_err",
             "-fflags", "+discardcorrupt+genpts+igndts",
             "-analyzeduration", "200M", "-probesize", "200M",
             "-i", ts_wsl, "-map", f"0:{stream_idx}",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=180)
        m = re.findall(r"frame=\s*(\d+)", r.stderr or "")
        if m:
            return max(int(x) for x in m)
    except Exception:
        return 0
    return 0


def per_pid_counts(ts_path: Path) -> dict:
    """Count packets per PID (TEI=0 only for ours; HDHR is all clean)."""
    from collections import Counter
    data = ts_path.read_bytes()
    n = len(data) // 188
    cnt = Counter()
    for i in range(n):
        b = i * 188
        if data[b] != 0x47:
            continue
        tei = (data[b+1] >> 7) & 1
        if tei:
            continue
        pid = ((data[b+1] & 0x1F) << 8) | data[b+2]
        cnt[pid] += 1
    return dict(cnt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True,
                    help='e.g. "v0 baseline gr-dtv native"')
    ap.add_argument("--decoder", default=None,
                    help="Custom decoder script (default: built-in gr-dtv native)")
    args = ap.parse_args()

    if not IQ_PATH.exists():
        sys.exit(f"baseline IQ missing: {IQ_PATH}")
    if not GT_TS.exists():
        sys.exit(f"HDHR ground-truth TS missing: {GT_TS}")

    print(f"Benchmark: {args.label}")
    print(f"  IQ:           {IQ_PATH}")
    print(f"  ground truth: {GT_TS}")

    work_dir = ROOT / "data" / "decoder_compare"
    cs16_path = work_dir / "_bench_in.cs16"
    ts_raw = work_dir / "_bench_raw.ts"
    ts_scr = work_dir / "_bench_scr.ts"

    # Convert IQ to CS16 once
    print("  Converting cf32 -> CS16...", flush=True)
    cs16_from_cf32(IQ_PATH, cs16_path)

    # Run decoder
    t0 = time.time()
    if args.decoder:
        print(f"  Running custom decoder: {args.decoder}")
        def w(p):
            s = str(p).replace("\\", "/")
            return "/mnt/c" + s[2:] if s[1:3] == ":/" else s
        subprocess.run(["wsl", "python3", w(args.decoder), w(cs16_path), w(ts_raw)],
                       check=True, timeout=600)
    else:
        print("  Running built-in gr-dtv native-rate decoder...", flush=True)
        run_grdtv_native(cs16_path, ts_raw)
    elapsed = time.time() - t0
    print(f"  Decode: {elapsed:.1f}s")

    # Scrub
    metrics = scrub_tei(ts_raw, ts_scr)
    print(f"  Clean packets: {metrics['clean']:,}/{metrics['total']:,} "
          f"({100*metrics['clean_frac']:.1f}%)")

    # Frame counts
    print("  Counting decoded frames...", flush=True)
    hd_ours = count_frames(ts_scr, stream_idx=2)
    sd_ours = count_frames(ts_scr, stream_idx=0)
    hd_gt   = count_frames(GT_TS, stream_idx=0)

    pid_ours = per_pid_counts(ts_scr)
    pid_gt   = per_pid_counts(GT_TS)
    # Matches: how many of HDHR's PIDs we also have, with what overlap fraction
    pid_overlap = {}
    for pid, gt_count in pid_gt.items():
        ours = pid_ours.get(pid, 0)
        pid_overlap[f"0x{pid:04X}"] = dict(gt=gt_count, ours=ours,
                                            ratio=round(ours / max(gt_count, 1), 3))

    result = {
        "label": args.label,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "decoder": args.decoder or "builtin_grdtv_native",
        "elapsed_sec": round(elapsed, 1),
        "rs_clean_frac": round(metrics["clean_frac"], 4),
        "rs_clean_count": metrics["clean"],
        "total_packets": metrics["total"],
        "hd_frames_ours": hd_ours,
        "hd_frames_groundtruth": hd_gt,
        "hd_frames_recovery_pct": round(100 * hd_ours / max(hd_gt, 1), 1),
        "sd_frames_ours": sd_ours,
        "pid_overlap": pid_overlap,
    }

    print()
    print("=== RESULT ===")
    print(f"  HD frames:        {hd_ours} / {hd_gt}  "
          f"({result['hd_frames_recovery_pct']}% of HDHR)")
    print(f"  SD frames:        {sd_ours}")
    print(f"  RS-clean:         {100*metrics['clean_frac']:.1f}%")
    print(f"  Top PID overlap:")
    for pid, info in sorted(pid_overlap.items(),
                             key=lambda kv: kv[1]["gt"], reverse=True)[:5]:
        print(f"    {pid}: ours/gt = {info['ours']}/{info['gt']} "
              f"({100*info['ratio']:.1f}%)")

    # Append to history
    history = []
    if RESULTS_FILE.exists():
        try:
            history = json.loads(RESULTS_FILE.read_text())
        except Exception:
            pass
    history.append(result)
    RESULTS_FILE.write_text(json.dumps(history, indent=2))
    print(f"\n  Appended to {RESULTS_FILE}")

    # Cleanup intermediates (keep ts_scr for inspection)
    cs16_path.unlink(missing_ok=True)
    ts_raw.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
