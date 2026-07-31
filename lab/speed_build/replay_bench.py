"""replay_bench.py — the offline gate for the speed-1 levers.

One deterministic tv_replay run = one row: the equalizer convergence curve
(fs_err_rms vs field-sync index), the decoded VIDEO FRAME count (ffmpeg
null-sink -map 0:v — ffprobe lies on multi-program TS), the output TS md5,
and the wall time.

Everything here is offline: no SDR, no daemon, no live chain. Usage:

  python lab/speed_build/replay_bench.py --iq lab/marginal_iq/rf34_ctrl.cs16 \
      --tag base --runs 3 --env STVT_EQ_RECYCLE=4

`--env K=V` may repeat. Results append to lab/speed_build/bench.jsonl.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PY = os.environ.get("STVT_PY", r"C:\Users\user\radioconda\python.exe")
OUT = REPO / "lab" / "speed_build" / "runs"
LEDGER = REPO / "lab" / "speed_build" / "bench.jsonl"

# The production chain env (day_program_729 BASE, minus the SDR-only knobs)
# plus full equalizer telemetry at EVERY field sync so the convergence curve
# has field resolution. STVT_EQ_LKG=1 mirrors tv_tuner.CHAIN_DEFAULTS.
BASE_ENV = {
    "STVT_VITERBI": "soft",
    "STVT_RS": "erasure",
    "STVT_SOVA": "1",
    "STVT_FPLL_FOLD": "1",
    "STVT_EQ": "long",
    "STVT_EQ_TELEM": "1",
    "STVT_EQ_TELEM_EVERY": "1",
    "STVT_EQ_LKG": "1",
    "STVT_EQ_LKG_RMS": "1.0",
}

RE_EQ = re.compile(r"\[eq-long t=\s*([\d.]+)s\] fs=(\d+) fs_err_rms=([\d.]+) \|taps\|=([\d.]+)")
RE_FRAME = re.compile(r"frame=\s*(\d+)")
RE_WARM = re.compile(r"\[eq-long\] WARM START")


def frames(ts: Path) -> int:
    """ffmpeg null-sink video frame count — the only trustworthy quality gauge."""
    if not ts.exists() or ts.stat().st_size < 2_000_000:
        return 0
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
         "-err_detect", "ignore_err", "-analyzeduration", "100M",
         "-probesize", "100M", "-i", str(ts), "-map", "0:v", "-f", "null", "-"],
        capture_output=True, text=True)
    m = RE_FRAME.findall(r.stderr)
    return int(m[-1]) if m else 0


def md5(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def curve(log: Path):
    """[(t, fs, err, |taps|)] from the eq-long telemetry lines."""
    rows = []
    for line in log.read_text(errors="replace").splitlines():
        m = RE_EQ.search(line)
        if m:
            rows.append((float(m.group(1)), int(m.group(2)),
                         float(m.group(3)), float(m.group(4))))
    return rows


def converge_field(rows, frac: float = 1.10):
    """First field-sync index whose err stays within `frac` x the final
    plateau (median of the last 25% of fields) for 5 consecutive fields.
    That is the honest 'cold convergence' number: how many fields until the
    equalizer is at the quality it will end at."""
    if len(rows) < 20:
        return None, None
    tail = sorted(r[2] for r in rows[int(len(rows) * 0.75):])
    plateau = tail[len(tail) // 2]
    thresh = plateau * frac
    run = 0
    for t, fs, err, _tp in rows:
        if err <= thresh:
            run += 1
            if run >= 5:
                return fs, plateau
        else:
            run = 0
    return None, plateau


def one_run(iq: Path, tag: str, i: int, extra: dict, cache_file: str | None):
    OUT.mkdir(parents=True, exist_ok=True)
    ts = OUT / f"{tag}_{i}.ts"
    # tv_replay redirects the CHAIN's stderr (where the C++ blocks fprintf
    # their telemetry) into its own --log file; our stdout capture only gets
    # the python logger. Parse the chain log.
    log = OUT / f"{tag}_{i}.log"
    chain = OUT / f"{tag}_{i}.chain.log"
    env = dict(os.environ)
    env.update(BASE_ENV)
    env.update(extra)
    env["PYTHONPATH"] = f"{REPO};{REPO / 'tools'}"
    if cache_file:
        env["STVT_EQ_TAP_CACHE_FILE"] = cache_file
    else:
        env.pop("STVT_EQ_TAP_CACHE_FILE", None)
        env.pop("STVT_EQ_TAP_CACHE", None)
    t0 = time.time()
    with open(log, "w", encoding="utf-8") as lf:
        subprocess.run([PY, str(REPO / "tools" / "tv_replay.py"),
                        "--iq", str(iq), "--out", str(ts), "--log", str(chain)],
                       cwd=str(REPO), env=env, stdout=lf,
                       stderr=subprocess.STDOUT, timeout=900)
    wall = time.time() - t0
    rows = curve(chain)
    cf, plateau = converge_field(rows)
    rec = {
        "tag": tag, "run": i, "iq": iq.name, "wall_s": round(wall, 2),
        "env": {k: v for k, v in extra.items()},
        "cache": cache_file, "frames": frames(ts), "md5": md5(ts),
        "ts_mb": round(ts.stat().st_size / 1e6, 1),
        "fields": len(rows), "converge_field": cf,
        "plateau_err": round(plateau, 4) if plateau else None,
        "warm_start": bool(RE_WARM.search(chain.read_text(errors="replace"))),
        "err_at": {str(n): (round(rows[n - 1][2], 4) if len(rows) >= n else None)
                   for n in (1, 5, 10, 20, 40, 80, 124, 200)},
    }
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iq", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--cache-file", default=None)
    # ── the valid gate (2026-07-29). See lab/gate_lib.py THE LAW: no decode
    # path here is bit-reproducible across processes, so a single-run md5
    # comparison is not a gate. --gate judges the runs collectively.
    ap.add_argument("--gate", action="store_true",
                    help="after the runs, apply the multi-run modal-hash / "
                         "frame-median gate from lab/gate_lib.py and exit "
                         "non-zero if it fails")
    ap.add_argument("--expect-frames", type=int, default=None)
    ap.add_argument("--expect-md5", action="append", default=[],
                    help="a hash (or 8-char prefix) this path is known to "
                         "produce; repeatable — the MODAL hash must be in the set")
    ap.add_argument("--frame-tol", type=int, default=2)
    a = ap.parse_args()
    extra = dict(kv.split("=", 1) for kv in a.env)
    iq = Path(a.iq) if os.path.isabs(a.iq) else REPO / a.iq
    recs = []
    for i in range(1, a.runs + 1):
        rec = one_run(iq, a.tag, i, extra, a.cache_file)
        recs.append(rec)
        print(f"[{a.tag} run {i}] frames={rec['frames']} md5={rec['md5'][:12]} "
              f"fields={rec['fields']} converge_field={rec['converge_field']} "
              f"plateau={rec['plateau_err']} warm={rec['warm_start']} "
              f"wall={rec['wall_s']}s", flush=True)

    if a.gate:
        sys.path.insert(0, str(REPO / "lab"))
        from gate_lib import RunRow, gate, render, SingleRunGateError
        rows = [RunRow(tag=r["tag"], run=r["run"], md5=r["md5"].upper(),
                       frames=r["frames"], wall_s=r["wall_s"]) for r in recs]
        try:
            res = gate(rows, name=f"replay_bench {a.tag}",
                       expect_md5=(a.expect_md5 or None),
                       expect_frames=a.expect_frames,
                       frame_tol=a.frame_tol)
        except SingleRunGateError as e:
            print(f"\nGATE REFUSED: {e}")
            return 2
        print()
        print(render(res))
        return 0 if res.passed else 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
