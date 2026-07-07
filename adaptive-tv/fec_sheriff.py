"""fec_sheriff.py — E5 v1: Reed-Solomon truth polices the equalizer.

Three convictions today shared one failure class: an adaptive layer
graded by a reference it can influence (v1.2 anchor mirage, the DFE's
confidently-wrong equilibrium, suspected RF15 DEAF). RS parity is the
reference no equalizer state can fake. The sheriff watches the chain
log for the CONFIDENTLY-WRONG SIGNATURE:

    fs_err says healthy (MER >= threshold)  AND
    RS says catastrophic (last-5s bad fraction >= 50%)  for 2 windows

and executes the equilibrium through the equalizer's command port
(STVT_EQ_CMD_FILE): dfe0 (zero + suspend feedback) then lkg (restore
known-good taps). Every action is logged as a SHERIFF event.

    python fec_sheriff.py --log <chain.log> --cmd <cmd file>
                          [--mer 15.0] [--badfrac 0.5] [--window 0]
"""
import argparse
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path

RE_FS = re.compile(r"fs_err_rms=([\d.]+)")
RE_RS5 = re.compile(
    r"\(last5s: pkts=(\d+) era_dec=\d+ era_ok=\d+ bad=(\d+)(?: sync=\d+)?\)")

ap = argparse.ArgumentParser()
ap.add_argument("--log", required=True)
ap.add_argument("--cmd", required=True)
ap.add_argument("--mer", type=float, default=15.0)
ap.add_argument("--badfrac", type=float, default=0.5)
ap.add_argument("--cooldown", type=float, default=15.0)
ap.add_argument("--vitcmd", default="", help="viterbi scalpel command file")
ap.add_argument("--window", type=int, default=0, help="exit after N s")
ap.add_argument("--jsonl", default=str(Path(__file__).parent / "cube_log.jsonl"))
args = ap.parse_args()

log = Path(args.log)
cmd = Path(args.cmd)
off = 0
recent_mers = []
guilty = 0
last_act = 0.0
t0 = time.time()
print(f"sheriff watching {log.name} (MER>={args.mer} + bad>={args.badfrac:.0%} "
      f"x2 -> dfe0+lkg)", flush=True)


acted_at = None      # time of last tap-surgery attempt (escalation timer)


def log_ev(action, reason):
    ev = {"event": "SHERIFF", "action": action, "reason": reason,
          "t": datetime.now().strftime("%H:%M:%S")}
    with open(args.jsonl, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev) + "\n")
    print(f"[{ev['t']}] SHERIFF: {reason} -> {action}", flush=True)


def act(reason):
    global last_act, acted_at
    if time.time() - last_act < args.cooldown:
        return
    last_act = time.time()
    acted_at = time.time()
    cmd.write_text("dfe0")
    time.sleep(0.4)                    # one FS poll consumes it
    if not cmd.exists():
        cmd.write_text("lkg")
    log_ev("dfe0+lkg", reason)


scalpel_used = False


def escalate(reason):
    """Tap surgery didn't cure it. Forensics (2026-07-06 23:00) left one
    un-alibied organ: viterbi internal state. Tier 2 = THE SCALPEL
    (full viterbi reset via STVT_VIT_CMD_FILE). Tier 3 = chain kill."""
    global acted_at, scalpel_used
    if args.vitcmd and not scalpel_used:
        scalpel_used = True
        acted_at = time.time()          # re-arm the escalation timer
        Path(args.vitcmd).write_text("reset")
        log_ev("SCALPEL (viterbi reset)", reason)
        return
    acted_at = None
    scalpel_used = False
    import psutil
    log_ev("CHAIN-KILL", reason)       # log FIRST — the kill ends our world
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if p.info["name"] == "python.exe" and any(
                    "tv_live" in a for a in (p.info["cmdline"] or [])):
                p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


while args.window == 0 or time.time() - t0 < args.window:
    time.sleep(2.0)
    try:
        size = log.stat().st_size
        if size < off:
            off = 0
            recent_mers.clear()
            guilty = 0
        with open(log, "r", errors="ignore") as f:
            f.seek(off)
            chunk = f.read()
            off = f.tell()
    except OSError:
        continue
    for e in RE_FS.findall(chunk):
        e = float(e)
        if e > 0:
            recent_mers.append(20 * math.log10(5.0 / e))
    recent_mers = recent_mers[-40:]
    for pk, bad in RE_RS5.findall(chunk):
        pk, bad = int(pk), int(bad)
        if pk < 5000:
            continue
        frac = bad / pk
        mer_now = (sorted(recent_mers)[len(recent_mers) // 2]
                   if len(recent_mers) >= 8 else None)
        if mer_now is not None and mer_now >= args.mer \
                and frac >= args.badfrac:
            # escalation: if tap surgery already ran and the next RS
            # window is STILL dead, the break is downstream — restart
            if acted_at is not None and time.time() - acted_at > 6:
                escalate(f"tap surgery failed, still {frac:.0%} RS fail")
                guilty = 0
                continue
            guilty += 1
            if guilty >= 2:
                act(f"confidently-wrong: MER {mer_now:.1f} "
                    f"with {frac:.0%} RS fail")
                guilty = 0
        else:
            if frac < 0.1:
                if acted_at is not None and scalpel_used:
                    log_ev("SCALPEL CURED IT", f"bad down to {frac:.0%}")
                acted_at = None        # cured — stand down escalation
                scalpel_used = False
            guilty = 0
