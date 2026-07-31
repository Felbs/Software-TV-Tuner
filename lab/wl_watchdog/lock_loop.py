"""lock_loop.py — the WL front-end LOCK-FAILURE loop harness.

Reproduces and measures the intermittent `atsc_wl_frontend` hard lock failure
recorded in lab/speed_build/WORKLOG.md §11.5(a): in a minority of otherwise
identical `tv_replay STVT_EQ=wl` runs the fused front end never achieves
timing lock — `relocks=0`, `segs_aligned=0 (0.00%)`, `fs accepted=0` — and
free-runs at exactly 1.5x (one output symbol per input SAMPLE instead of per
symbol), so `segs_emitted` comes out 291044 instead of 194030 and the TS is
0 bytes.

One deterministic fixture, N identical runs, lock telemetry per run:

  python lab/wl_watchdog/lock_loop.py --runs 30 --tag before
  python lab/wl_watchdog/lock_loop.py --runs 30 --tag after --keep-logs

`--debug` adds ATSC_SYNC_SOFT_DEBUG=1 so a failing run's per-256-segment
correlator state (peak / rms / snr_ratio / best_idx) lands in the log —
that is the diagnosis channel; it costs ~1100 stderr lines per run.

Failing-run logs are ALWAYS kept (lab/wl_watchdog/runs/<tag>_fail_<i>.log);
passing-run logs are deleted unless --keep-logs.

Results append to lab/wl_watchdog/loop.jsonl.
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
OUT = REPO / "lab" / "wl_watchdog" / "runs"
LEDGER = REPO / "lab" / "wl_watchdog" / "loop.jsonl"

# The WL replay env: production arsenal + STVT_EQ=wl (which requires FOLD=1).
# Deliberately the SAME env the §11.5(a) measurement used.
WL_ENV = {
    "STVT_VITERBI": "soft",
    "STVT_RS": "erasure",
    "STVT_SOVA": "1",
    "STVT_FPLL_FOLD": "1",
    "STVT_EQ": "wl",
    "STVT_EQ_TELEM": "1",
}

# [wl_front FINAL] segs_emitted=194030 segs_held=0 segs_aligned=193893 (99.93%)
#   relocks=1 | fs accepted=620 rejected_early=0 rejected_late=0 coasted=0
RE_FINAL = re.compile(
    r"\[wl_front FINAL\] segs_emitted=(\d+) segs_held=(\d+) "
    r"segs_aligned=(\d+) \(([\d.]+)%\) relocks=(\d+) \| fs accepted=(\d+) "
    r"rejected_early=(\d+) rejected_late=(\d+) coasted=(\d+)")
RE_DBG = re.compile(
    r"\[wl_front\] seg=(\d+) peak=(\S+) rms=(\S+) snr_ratio=(\S+) "
    r"locked=(\d+) best_idx=(-?\d+)")
RE_WD = re.compile(r"\[wl_front wd\].*")
RE_WDF = re.compile(
    r"\[wl_front WD FINAL\] wd=(\d+) window=(\d+) max=(\d+) resets=(\d+) "
    r"gave_up=(\d+) recovered=(\d+) first_align_seg=(\d+) first_fs_seg=(\d+) "
    r"segs_seen=(\d+)")
# STVT_WL_PROBE_DIAG=1 — the latent-hazard measurement (see the .cc comment)
RE_PROBE = re.compile(
    r"\[wl_front probe\] unpadded_addr=(\S+) addr_mod32=(\d+) "
    r"unpadded_nonfinite=(\d+)/(\d+) unpadded_would_have_failed=(\d+)")
RE_ELAPSED = re.compile(r"DONE elapsed=([\d.]+)s")


def md5(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def one_run(iq: Path, tag: str, i: int, extra_env: dict, debug: bool,
            keep_logs: bool, probe_diag: bool = False) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    ts = OUT / f"{tag}_{i:03d}.ts"
    log = OUT / f"{tag}_{i:03d}.log"
    env = dict(os.environ)
    env.update(WL_ENV)
    env.update(extra_env)
    if debug:
        env["ATSC_SYNC_SOFT_DEBUG"] = "1"
    if probe_diag:
        env["STVT_WL_PROBE_DIAG"] = "1"
    t0 = time.time()
    subprocess.run([PY, str(REPO / "tools" / "tv_replay.py"),
                    "--iq", str(iq), "--out", str(ts), "--log", str(log)],
                   env=env, cwd=str(REPO), capture_output=True, text=True)
    wall = time.time() - t0

    txt = log.read_text(errors="replace") if log.exists() else ""
    row: dict = {"tag": tag, "run": i, "iq": iq.name, "wall_s": round(wall, 1),
                 "ts_bytes": ts.stat().st_size if ts.exists() else 0}
    m = RE_FINAL.search(txt)
    if m:
        row.update(segs_emitted=int(m.group(1)), segs_held=int(m.group(2)),
                   segs_aligned=int(m.group(3)), aligned_pct=float(m.group(4)),
                   relocks=int(m.group(5)), fs_accepted=int(m.group(6)),
                   fs_rej_early=int(m.group(7)), fs_rej_late=int(m.group(8)),
                   coasted=int(m.group(9)))
    else:
        row["no_final_line"] = True
    wd = RE_WD.findall(txt)
    if wd:
        row["watchdog_lines"] = wd[:40]
        row["watchdog_n"] = len(wd)
    m = RE_WDF.search(txt)
    if m:
        row.update(wd_resets=int(m.group(4)), wd_gave_up=int(m.group(5)),
                   wd_recovered=int(m.group(6)),
                   first_align_seg=int(m.group(7)),
                   first_fs_seg=int(m.group(8)))
    m = RE_PROBE.search(txt)
    if m:
        row.update(probe_addr_mod32=int(m.group(2)),
                   probe_nonfinite=int(m.group(3)),
                   probe_would_have_failed=int(m.group(5)))
    # classify
    row["lock_fail"] = bool(row.get("fs_accepted", 0) == 0
                            or row.get("aligned_pct", 0.0) < 1.0
                            or row["ts_bytes"] == 0)
    if row["lock_fail"]:
        # the diagnosis channel: first 12 debug samples of the correlator state
        dbg = RE_DBG.findall(txt)
        row["dbg_first"] = dbg[:12]
        row["dbg_last"] = dbg[-3:]
        fail_log = OUT / f"{tag}_fail_{i:03d}.log"
        fail_log.write_text(txt, errors="replace")
        row["fail_log"] = fail_log.name
    else:
        row["ts_md5"] = md5(ts)
    if ts.exists() and not keep_logs:
        ts.unlink()
    if log.exists() and not keep_logs:
        log.unlink()
    with open(LEDGER, "a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iq", default=str(REPO / "lab" / "marginal_iq" / "rf34_ctrl.cs16"))
    ap.add_argument("--runs", type=int, default=30)
    ap.add_argument("--tag", default="loop")
    ap.add_argument("--env", action="append", default=[],
                    help="extra K=V for the child (repeatable)")
    ap.add_argument("--debug", action="store_true",
                    help="ATSC_SYNC_SOFT_DEBUG=1 (correlator state in the log)")
    ap.add_argument("--probe-diag", action="store_true",
                    help="STVT_WL_PROBE_DIAG=1 — also re-enact the PRE-FIX "
                         "unpadded probe and record whether it would have "
                         "poisoned the taps table this run (the latent-hazard "
                         "rate; must match the historical failure rate)")
    ap.add_argument("--keep-logs", action="store_true")
    ap.add_argument("--stop-on-fail", action="store_true")
    a = ap.parse_args()

    extra = {}
    for kv in a.env:
        k, _, v = kv.partition("=")
        extra[k] = v

    iq = Path(a.iq).resolve()
    fails = 0
    print(f"# lock_loop tag={a.tag} runs={a.runs} iq={iq.name} extra={extra} "
          f"debug={a.debug}", flush=True)
    for i in range(1, a.runs + 1):
        r = one_run(iq, a.tag, i, extra, a.debug, a.keep_logs, a.probe_diag)
        if r["lock_fail"]:
            fails += 1
        print(f"[{i:3d}/{a.runs}] {'FAIL' if r['lock_fail'] else 'ok  '} "
              f"emitted={r.get('segs_emitted','?')} "
              f"aligned={r.get('aligned_pct','?')}% "
              f"relocks={r.get('relocks','?')} fs={r.get('fs_accepted','?')} "
              f"wd={r.get('wd_resets',0)}/{r.get('watchdog_n',0)} "
              f"fs1={r.get('first_fs_seg','?')} "
              f"latent={r.get('probe_would_have_failed','-')} "
              f"ts={r['ts_bytes']} {r.get('ts_md5','')[:8]} "
              f"({r['wall_s']}s)  running_fails={fails}", flush=True)
        if r["lock_fail"] and a.stop_on_fail:
            break
    print(f"\n== {a.tag}: {fails}/{a.runs} lock failures "
          f"({100.0*fails/max(1,a.runs):.1f}%) ==", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
