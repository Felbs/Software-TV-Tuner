"""front_only.py — atsc_wl_frontend in ISOLATION on byte-frozen input.

Companion to dump_front_in.py. Graph:

    file_source(front_in_r.f32) --> in0 \
                                        atsc_wl_frontend --> 3 null sinks
    file_source(front_in_i.f32) --> in1 /

The front end's input is now the SAME BYTES every run, so any run-to-run
difference in its lock telemetry is produced INSIDE the block (work-call
boundary / chunking / uninitialised state) and nowhere else.

  python lab/wl_watchdog/front_only.py --runs 60

Prints one line per run and a failure count. `--runs 0` runs a single in-process
pass (used by the loop as the child).
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PY = os.environ.get("STVT_PY", os.path.join(os.environ.get("USERPROFILE", ""), "radioconda", "python.exe"))

RE_FINAL = re.compile(
    r"\[wl_front FINAL\] segs_emitted=(\d+) segs_held=(\d+) "
    r"segs_aligned=(\d+) \(([\d.]+)%\) relocks=(\d+) \| fs accepted=(\d+)")


def child(rp: Path, ip: Path) -> int:
    from gnuradio import gr, blocks                     # noqa: E402
    from gnuradio import atscplus                       # noqa: E402
    from gnuradio.dtv.atsc_rx_filter import ATSC_SYMBOL_RATE   # noqa: E402

    rate = ATSC_SYMBOL_RATE * float(os.environ.get("STVT_SPS", "1.5"))
    tb = gr.top_block("wl front only")
    fr = blocks.file_source(gr.sizeof_float, str(rp), False)
    fi = blocks.file_source(gr.sizeof_float, str(ip), False)
    wf = atscplus.atsc_wl_frontend(rate)
    # WORK-CALL CHUNKING CONTROLS — the whole point of the isolation rig. The
    # input is byte-frozen, so chunking is the ONLY remaining variable; these
    # let it be swept deterministically instead of waited for.
    mno = int(os.environ.get("WLF_MAX_NOUTPUT", "0"))
    if mno:
        wf.set_max_noutput_items(mno)
    mib = int(os.environ.get("WLF_IN_BUF", "0"))
    if mib:
        for b in (fr, fi):
            b.set_min_output_buffer(mib)
    mob = int(os.environ.get("WLF_OUT_BUF", "0"))
    if mob:
        wf.set_min_output_buffer(mob)
    osig = wf.output_signature()
    n0 = blocks.null_sink(osig.sizeof_stream_item(0))
    n1 = blocks.null_sink(osig.sizeof_stream_item(1))
    n2 = blocks.null_sink(osig.sizeof_stream_item(2))
    tb.connect(fr, (wf, 0))
    tb.connect(fi, (wf, 1))
    tb.connect((wf, 0), n0)
    tb.connect((wf, 1), n1)
    tb.connect((wf, 2), n2)
    tb.start()
    tb.wait()
    del tb          # force the block dtor so [wl_front FINAL] prints
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default="front_in")
    ap.add_argument("--runs", type=int, default=60)
    ap.add_argument("--child", action="store_true")
    ap.add_argument("--env", action="append", default=[])
    a = ap.parse_args()
    rp = HERE / f"{a.prefix}_r.f32"
    ip = HERE / f"{a.prefix}_i.f32"
    if a.child:
        return child(rp, ip)

    if not rp.exists():
        print(f"missing {rp} — run dump_front_in.py first", file=sys.stderr)
        return 2
    extra = {}
    for kv in a.env:
        k, _, v = kv.partition("=")
        extra[k] = v
    env = dict(os.environ)
    env.update(extra)
    nsym = rp.stat().st_size // 4
    print(f"# front_only runs={a.runs} input={nsym} samples/plane extra={extra}")
    fails = 0
    rows = []
    for i in range(1, a.runs + 1):
        r = subprocess.run([PY, str(Path(__file__).resolve()), "--child",
                            "--prefix", a.prefix],
                           env=env, cwd=str(REPO), capture_output=True, text=True)
        m = RE_FINAL.search(r.stderr)
        if not m:
            print(f"[{i:3d}] NO FINAL LINE\n{r.stderr[-2000:]}")
            fails += 1
            continue
        emitted, held, aligned, pct, relocks, fs = (
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            float(m.group(4)), int(m.group(5)), int(m.group(6)))
        bad = (fs == 0 or pct < 1.0)
        fails += bad
        rows.append((emitted, pct, relocks, fs))
        wd = [l for l in r.stderr.splitlines() if "[wl_front wd" in l]
        print(f"[{i:3d}/{a.runs}] {'FAIL' if bad else 'ok  '} emitted={emitted} "
              f"aligned={pct}% relocks={relocks} fs={fs} wd={len(wd)}"
              + (f"  {wd[0][:110]}" if wd else ""), flush=True)
    print(f"\n== front_only: {fails}/{a.runs} lock failures "
          f"({100.0*fails/max(1,a.runs):.1f}%) ==")
    if rows:
        uniq = sorted(set(rows))
        print(f"distinct telemetry tuples ({len(uniq)}): {uniq[:10]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
