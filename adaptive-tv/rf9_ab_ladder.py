"""RF9 morning investigation — direct-chain A/B ladder (2026-07-11).

Arms (RF9, Antenna B, rfgain_sel 5, IFGR 28, AGC on, play-path env):
  1 BASE  (turbo on,  erasures 0)      4 T0    (turbo off)
  2 T0    (turbo off, erasures 0)      5 CLIFF (arsenal)
  3 CLIFF (turbo on,  erasure arsenal) 6 BASE  (turbo on)
ABBA-ish so RF9's breathing shows up as per-arm MER, not config bias.
Panel process must be DEAD (sweeper steals the radio; flight recorder
reads live.ts).  No players attached: chain-only loss.
"""
import sys
import io, json, math, os, re, subprocess, sys, time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

PY = sys.executable
HERE = Path(r"Z:\src\adaptive-tv")
TOOLS = Path(r"Z:\src\magic-tv-decoder\tools")
LIVE = TOOLS / "data" / "tv_live" / "live.ts"
OUT = HERE / "lab" / "rf9_ab_ladder.jsonl"

RE_FS = re.compile(r"fs_err_rms=([\d.]+)")
RE_RS5 = re.compile(r"last5s: pkts=(\d+) era_dec=\d+ era_ok=\d+ bad=(\d+)")
RE_TURBO = re.compile(r"\[rs_turbo t=\s*([\d.]+)s\] att=(\d+) retry=\d+ "
                      r"resc=(\d+) bytes=\d+ syms=(\d+) skip=(\d+) "
                      r"selftest=\d+/\d+ fail_ema=([\d.]+)%")

def base_env(turbo="1", cliff=False):
    env = os.environ.copy()
    env["PATH"] = (r"C:\Program Files\SDRplay\API\x64;C:\ffmpeg\bin;"
                   + env.get("PATH", ""))
    env.update({
        "STVT_ANTENNA": "Antenna B", "STVT_IFGR": "28",
        "STVT_RFGAIN_SEL": "5", "STVT_RF": "9",
        "STVT_SDR_AGC": "1", "STVT_AGC_SETPOINT": "-20",
        "STVT_EQ": "long", "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1",
        "STVT_DABNOTCH": "0",                       # rf < 14
        "STVT_RS": "erasure", "STVT_RS_ERASURES": "0",
        "STVT_SOVA": "1", "STVT_TURBO": turbo,
        "STVT_EQ_MOD12_GUARD": "1",
        "STVT_SPS": "1.1", "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1",
        "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
        "STVT_EQ_TELEM": "1", "STVT_EQ_CIR": "1",
        "STVT_IQ_RING": "0",
        "STVT_EQ_TAP_CACHE": str(HERE / "lab" / "tapcache"),
    })
    env.pop("STVT_PERSIST_RETUNE", None)
    if cliff:
        env.update({"STVT_RS_ERASURES": "14", "STVT_EQ_DFE": "1",
                    "STVT_EQ_DFE_ANCHOR": "1", "STVT_EQ_RESEED": "1",
                    "STVT_EQ_QUALITY_BAD_RMS": "8"})
    return env

def kill_chain():
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    "Where-Object { $_.CommandLine -match 'tv_live' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                    "-ErrorAction SilentlyContinue }"], capture_output=True)
    time.sleep(2)

def parse_window(text):
    rs5 = RE_RS5.findall(text)
    pk = sum(int(p) for p, _ in rs5)
    bd = sum(int(b) for _, b in rs5)
    errs = [float(m.group(1)) for m in RE_FS.finditer(text)]
    mers = sorted(20 * math.log10(5.0 / e) for e in errs if e > 0)
    tb = RE_TURBO.findall(text)
    turbo = None
    if len(tb) >= 2:
        t0, t1 = tb[0], tb[-1]
        dt = float(t1[0]) - float(t0[0])
        if dt > 0:
            turbo = {"att_s": round((int(t1[1]) - int(t0[1])) / dt, 1),
                     "resc": int(t1[2]) - int(t0[2]),
                     "syms_s": round((int(t1[3]) - int(t0[3])) / dt),
                     "skip": int(t1[4]) - int(t0[4]),
                     "fail_ema_last": float(t1[5])}
    return {"pkts": pk, "bad": bd,
            "loss_pct": round(100.0 * bd / pk, 3) if pk else None,
            "oso": text.count("OsO"),
            "mod12": text.count("MOD12 SLIP"),
            "mer_med": round(mers[len(mers) // 2], 2) if mers else None,
            "mer_mean": round(sum(mers) / len(mers), 2) if mers else None,
            "mer_p10": round(mers[max(0, len(mers) // 10 - 1)], 2)
                       if mers else None,
            "rs5_windows": len(rs5), "turbo": turbo}

def run_arm(idx, name, env, settle=30, measure=150):
    for attempt in (1, 2):
        kill_chain()
        try:
            if LIVE.exists():
                LIVE.unlink()
        except OSError:
            pass
        log_path = HERE / "lab" / f"rf9_arm_{idx}_{name}.log"
        lf = open(log_path, "w")
        proc = subprocess.Popen([PY, "-u", str(TOOLS / "tv_live.py"),
                                 "--rf", "9"], env=env,
                                stdout=lf, stderr=subprocess.STDOUT)
        # wait for equalizer telemetry = lock
        t0 = time.time()
        locked = False
        while time.time() - t0 < 100:
            time.sleep(5)
            try:
                sz = log_path.stat().st_size
                with open(log_path, "r", errors="ignore") as f:
                    f.seek(max(0, sz - 20000))
                    if "fs_err_rms=" in f.read():
                        locked = True
                        break
            except OSError:
                pass
        if not locked:
            print(f"[arm {idx} {name}] attempt {attempt}: NO LOCK in 100 s "
                  f"— dud, retrying" if attempt == 1 else
                  f"[arm {idx} {name}] DUD twice — recording dud", flush=True)
            proc.terminate()
            if attempt == 2:
                return {"arm": idx, "name": name, "dud": True}
            continue
        time.sleep(settle)
        ofs = log_path.stat().st_size
        time.sleep(measure)
        with open(log_path, "r", errors="ignore") as f:
            f.seek(ofs)
            text = f.read()
        kill_chain()
        res = {"arm": idx, "name": name,
               "t_start": time.strftime("%H:%M:%S", time.localtime(t0)),
               "measure_s": measure}
        res.update(parse_window(text))
        return res

ARMS = [(1, "BASE_T1", dict(turbo="1", cliff=False)),
        (2, "TURBO_OFF", dict(turbo="0", cliff=False)),
        (3, "CLIFF", dict(turbo="1", cliff=True)),
        (4, "TURBO_OFF", dict(turbo="0", cliff=False)),
        (5, "CLIFF", dict(turbo="1", cliff=True)),
        (6, "BASE_T1", dict(turbo="1", cliff=False))]

def main():
    print(f"ladder start {time.strftime('%H:%M:%S')}", flush=True)
    for idx, name, kw in ARMS:
        res = run_arm(idx, name, base_env(**kw))
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(res) + "\n")
        print(json.dumps(res), flush=True)
    kill_chain()
    print(f"ladder done {time.strftime('%H:%M:%S')}", flush=True)

if __name__ == "__main__":
    main()
