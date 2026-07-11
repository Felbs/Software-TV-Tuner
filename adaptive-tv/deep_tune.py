"""deep_tune.py — DEEP TUNE, the channel doctor.

One button, one question: what is the best this channel can be RIGHT NOW,
and what is actually wrong with it?  Phases:

  1. baseline   — measure the current antenna/gains (MER median + p10 from
                  fs_err_rms telemetry, loss%% from the RS last5s windows,
                  source-overflow count)
  2. antenna race — same channel on every antenna the panel knows;
                  no lock in ~25 s = honest "no lock", not an error
  3. gain refinement — small (rfgain_sel x IFGR) grid around the current
                  seed on the winning antenna (mer_gain_cal's idea, bounded).
                  The AGC servo tracks IF gain live; the REGIME + SEED are
                  what we are choosing, so cells run with the play-path env.
  4. disease classification — the documented taxonomy (below-cliff, fader,
                  impulse, overload, plumbing, healthy)
  5. recipe     — lab/channel_recipes.json; the panel's tune path consults
                  it (recipe overrides the static GAINS row; a user antenna
                  pick always outranks the recipe antenna)

POLLING LAW: this module never reads live.ts and never hammers the chain —
metrics come from sparse reads of the chain log (the tail is read once per
measurement window; the lock-wait reads a tiny file every 3 s).

Standalone:
    python deep_tune.py --rf 15 [--secs 45] [--ants "Antenna A,Antenna B"]
Panel: tv_tuna_panel.py wires DeepTune with its own env/status/cancel hooks.
"""
import argparse
import io
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = Path(r"Z:\src\magic-tv-decoder\tools")
PY = r"C:\Users\user\radioconda\python.exe"
RECIPES = HERE / "lab" / "channel_recipes.json"

CLIFF = 15.2            # the survival-curve cliff (dB)
STALE_DAYS = 14         # recipes older than this are used but flagged

RE_FS = re.compile(r"fs_err_rms=([\d.]+)")
RE_RS5 = re.compile(r"last5s: pkts=(\d+) era_dec=\d+ era_ok=\d+ bad=(\d+)")
RE_MAXX = re.compile(r"max\|x\|=([\d.]+)")
RE_INRMS = re.compile(r"in_rms=([\d.]+)")
RE_OSO = re.compile(r"OsO|^O", re.M)     # the panel's overflow metric


# ── recipes ─────────────────────────────────────────────────────────
def load_recipes(path=RECIPES):
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def load_recipe(rf, path=RECIPES):
    r = load_recipes(path).get(str(rf))
    return r if isinstance(r, dict) else None


def save_recipe(rf, rec, path=RECIPES):
    path = Path(path)
    d = load_recipes(path)
    d[str(rf)] = rec
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def recipe_age_days(rec):
    try:
        return max(0.0, (datetime.now()
                         - datetime.fromisoformat(rec["ts"])).total_seconds()
                   / 86400.0)
    except (KeyError, TypeError, ValueError):
        return 0.0


# ── measurement ─────────────────────────────────────────────────────
def window_metrics(text):
    """Everything DEEP TUNE knows, from one window of chain-log text."""
    out = {"n": 0, "mer_med": None, "mer_p10": None, "loss_pct": None,
           "bursty": False, "spikes": 0, "oso": 0, "in_rms": None}
    mers = sorted(20.0 * math.log10(5.0 / float(m.group(1)))
                  for m in RE_FS.finditer(text) if float(m.group(1)) > 0)
    out["n"] = len(mers)
    if mers:
        out["mer_med"] = round(mers[len(mers) // 2], 2)
        out["mer_p10"] = round(mers[len(mers) // 10], 2)
    rs = RE_RS5.findall(text)
    if rs:
        pk = sum(int(p) for p, _ in rs)
        bd = sum(int(b) for _, b in rs)
        if pk:
            out["loss_pct"] = round(100.0 * bd / pk, 3)
        bad = [int(b) for _, b in rs]
        hot = sum(1 for b in bad if b > 0)
        # bursty = the loss lives in a minority of the 5 s windows
        out["bursty"] = bd > 0 and len(bad) >= 4 and hot <= max(1, len(bad) // 3)
    out["spikes"] = sum(1 for m in RE_MAXX.finditer(text)
                        if float(m.group(1)) > 1.5)
    ir = RE_INRMS.findall(text)
    if ir:
        out["in_rms"] = float(ir[-1])
    out["oso"] = len(RE_OSO.findall(text))
    return out


def classify(m, overload=False):
    """(disease, plain-language description) from the winner's metrics."""
    if m is None or m.get("mer_med") is None:
        return ("no-lock", "no lock on any antenna — wrong hour, or the "
                           "signal simply is not reaching this room")
    med, p10 = m["mer_med"], m.get("mer_p10")
    loss = m.get("loss_pct")
    if med < CLIFF:
        return ("below-cliff",
                f"below the cliff everywhere (best MER {med:.1f} < 15.2) — "
                "antenna/aperture problem or the wrong hour")
    if overload:
        return ("overload", "overload — MER falls as gain rises; "
                            "the recipe backs the gain off")
    if loss is None or loss < 0.3:
        return ("healthy", "nothing wrong — channel is healthy right now")
    if p10 is not None and p10 < CLIFF:
        return ("fader", f"fader — MER median {med:.1f} is fine but dips to "
                         f"{p10:.1f} (under the cliff); the loss comes in fades")
    if m.get("spikes", 0) >= 3 and m.get("bursty"):
        return ("impulse", "impulse noise — healthy MER, loss in bursts, "
                           "FPLL |x| spikes (something is arcing/switching)")
    if m.get("oso", 0) > 0 and not m.get("bursty"):
        return ("plumbing", "plumbing/realtime — healthy MER but steady loss "
                            "with source overflows; the computer, not the sky")
    return ("glitchy", f"bursty loss ({loss:.2f}%) with healthy MER — "
                       "interference of an unclassified shape")


def fmt(m):
    if m is None:
        return "no lock"
    return (f"MER {m['mer_med']:.1f}/p10 "
            f"{(m['mer_p10'] if m['mer_p10'] is not None else 0):.1f} dB, "
            f"loss {m['loss_pct'] if m['loss_pct'] is not None else '?'}%")


class Cancelled(Exception):
    pass


# ── the engine ──────────────────────────────────────────────────────
class DeepTune:
    """Callable from the panel thread or the CLI. All environment/side
    effects are injected so the engine itself stays dumb and testable:

      env_builder(antenna, rfgain_sel, ifgr) -> env dict for tv_live.py
      status(pct, msg)          progress line (plain ASCII msg)
      cancelled() -> bool       polled between sleeps; True aborts cleanly
      record(ant, metrics)      quality-history hook (source=deeptune)
      hour_hint() -> str|None   Knob-of-Time advice for the verdict
    """

    def __init__(self, rf, antennas, cur_ant, cur_gains, env_builder,
                 log_path, status=None, cancelled=None, record=None,
                 hour_hint=None, base_secs=45, race_secs=45, cell_secs=20,
                 recipes_path=RECIPES):
        self.rf = int(rf)
        self.antennas = list(antennas)
        self.cur_ant = cur_ant
        self.cur_gains = (int(cur_gains[0]), int(cur_gains[1]))
        self.env_builder = env_builder
        self.log_path = Path(log_path)
        self.status = status or (lambda pct, msg: None)
        self.cancelled = cancelled or (lambda: False)
        self.record = record or (lambda ant, m: None)
        self.hour_hint = hour_hint or (lambda: None)
        self.base_secs, self.race_secs, self.cell_secs = \
            base_secs, race_secs, cell_secs
        self.recipes_path = recipes_path
        self.proc = None

    # — chain control —
    def _kill(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=6)
            except Exception:
                self.proc.kill()
        self.proc = None

    def _spawn(self, env):
        self._kill()
        time.sleep(2)                     # let the SDR session release
        self._cancel_point()
        lf = open(self.log_path, "w")
        self.proc = subprocess.Popen(
            [PY, "-u", str(TOOLS / "tv_live.py"), "--rf", str(self.rf)],
            env=env, stdout=lf, stderr=subprocess.STDOUT)
        lf.close()
        if self.cancelled():          # a user tune landed mid-spawn: never
            self._kill()              # leave a competing chain behind
            raise Cancelled()

    def _cancel_point(self):
        if self.cancelled():
            raise Cancelled()

    def _read(self, ofs=0):
        try:
            with open(self.log_path, "r", errors="ignore") as f:
                f.seek(ofs)
                return f.read()
        except OSError:
            return ""

    # — one measurement window (sparse polling; log read once at the end) —
    def _measure(self, secs, label, pct):
        t0 = time.time()
        locked = False
        while time.time() - t0 < 25:
            self._cancel_point()
            time.sleep(3)
            txt = self._read()
            if ("sdrplay_api_Fail" in txt or "Init() failed" in txt
                    or "no available RSP" in txt):
                raise RuntimeError("radio failed to open (SDRplay init) — "
                                   "restart the API service / replug")
            if RE_FS.search(txt):
                locked = True
                break
        if not locked:
            return None
        time.sleep(6)                     # skip the convergence transient
        try:
            ofs = os.path.getsize(self.log_path)
        except OSError:
            ofs = 0
        t1 = time.time()
        while time.time() - t1 < secs:
            self._cancel_point()
            left = secs - int(time.time() - t1)
            self.status(pct, f"{label} - {left}s left")
            time.sleep(min(6, max(1, left)))
        return window_metrics(self._read(ofs))

    # — winner picking: MER median first, loss as the sanity check —
    @staticmethod
    def _pick(table):
        cands = [(a, m) for a, m in table.items()
                 if m and m.get("mer_med") is not None]
        if not cands:
            return None, None
        cands.sort(key=lambda am: am[1]["mer_med"], reverse=True)
        best = cands[0]
        for a, m in cands[1:]:
            # a near-tie with clearly less loss is the better recipe
            if (best[1]["mer_med"] - m["mer_med"] <= 0.4
                    and (m.get("loss_pct") or 0)
                    < 0.5 * (best[1].get("loss_pct") or 0)):
                best = (a, m)
        return best

    def run(self):
        try:
            return self._run()
        except Cancelled:
            return {"status": "cancelled"}
        finally:
            self._kill()

    def _run(self):
        rf = self.rf
        table = {}
        # 1 — baseline
        self.status(3, f"baseline: {self.cur_ant} rfgain "
                       f"{self.cur_gains[0]}/IFGR {self.cur_gains[1]}")
        self._spawn(self.env_builder(self.cur_ant, *self.cur_gains))
        base = self._measure(self.base_secs, f"baseline {self.cur_ant}", 12)
        table[self.cur_ant] = base
        if base:
            self.record(self.cur_ant, base)
        # 2 — antenna race
        others = [a for a in self.antennas if a != self.cur_ant]
        for i, ant in enumerate(others):
            pct = 22 + int(28 * i / max(1, len(others)))
            self.status(pct, f"antenna race: {ant}")
            self._spawn(self.env_builder(ant, *self.cur_gains))
            met = self._measure(self.race_secs, f"antenna race {ant}", pct + 8)
            table[ant] = met
            if met:
                self.record(ant, met)
        win_ant, win_met = self._pick(table)
        if win_met is None:
            _, dtext = classify(None)
            hint = self.hour_hint()
            verdict = dtext + (f"; {hint}" if hint else "")
            return {"status": "done", "verdict": verdict, "table": table,
                    "winner": None, "recipe": None, "grid": []}
        # 3 — gain refinement on the winner (bounded grid, play-path env)
        r0, i0 = self.cur_gains
        rss = sorted({max(0, r0 - 1), r0, min(9, r0 + 1)})
        ifs = sorted({max(20, i0 - 4), i0, min(59, i0 + 4)})
        cells = [(r, i) for r in rss for i in ifs if (r, i) != (r0, i0)]
        grid = []
        best_gains, best_met = (r0, i0), win_met
        for k, (r, i) in enumerate(cells):
            pct = 55 + int(35 * k / max(1, len(cells)))
            self.status(pct, f"gain grid on {win_ant}: rfgain {r}/IFGR {i} "
                             f"({k + 1}/{len(cells)})")
            self._spawn(self.env_builder(win_ant, r, i))
            met = self._measure(self.cell_secs, f"gain {r}/{i}", pct + 3)
            grid.append({"rfgain_sel": r, "ifgr": i, "met": met})
            if met and met.get("mer_med") is not None:
                loss_ok = ((met.get("loss_pct") or 0)
                           <= max(0.5, 2.0 * (win_met.get("loss_pct") or 0)))
                if met["mer_med"] >= best_met["mer_med"] + 0.3 and loss_ok:
                    best_gains, best_met = (r, i), met
        # overload check: does LESS gain measure clearly better?
        pts = [(g["rfgain_sel"] * 10 + g["ifgr"], g["met"]["mer_med"])
               for g in grid if g["met"] and g["met"].get("mer_med") is not None]
        pts.append((r0 * 10 + i0, win_met["mer_med"]))
        overload = False
        if len(pts) >= 3:
            pts.sort()
            overload = pts[-1][1] - pts[0][1] >= 1.0
        # 4 — disease
        disease, dtext = classify(best_met, overload)
        # 5 — recipe
        recipe = {"antenna": win_ant, "rfgain_sel": best_gains[0],
                  "ifgr": best_gains[1], "measured_mer": best_met["mer_med"],
                  "measured_loss": best_met.get("loss_pct"),
                  "disease": disease,
                  "ts": datetime.now().replace(microsecond=0).isoformat()}
        save_recipe(rf, recipe, self.recipes_path)
        self.record(win_ant, best_met)
        # verdict, in plain language with the numbers that matter
        parts = []
        if win_ant != self.cur_ant and base and base.get("mer_med") is not None:
            d = win_met["mer_med"] - base["mer_med"]
            parts.append(f"{win_ant} wins ({d:+.1f} dB over {self.cur_ant})")
        elif win_ant != self.cur_ant:
            parts.append(f"{win_ant} wins ({self.cur_ant}: no lock)")
        else:
            parts.append(f"{win_ant} confirmed best")
        if best_gains != (r0, i0):
            parts.append(f"gains refreshed to rfgain {best_gains[0]}/IFGR "
                         f"{best_gains[1]} ({fmt(best_met)})")
        else:
            parts.append(f"gains confirmed ({fmt(best_met)})")
        parts.append(f"disease: {disease} - {dtext}")
        hint = self.hour_hint()
        if hint:
            parts.append(hint)
        self.status(100, "done")
        return {"status": "done", "verdict": "; ".join(parts),
                "winner": {"antenna": win_ant, "gains": best_gains,
                           "met": best_met}, "table": table, "grid": grid,
                "recipe": recipe, "disease": disease}


# ── standalone CLI ──────────────────────────────────────────────────
def cli_env(rf, ant, rsel, ifgr):
    env = os.environ.copy()
    env["PATH"] = (r"C:\Program Files\SDRplay\API\x64;C:\ffmpeg\bin;"
                   + env.get("PATH", ""))
    env.update({
        "STVT_ANTENNA": ant, "STVT_RFGAIN_SEL": str(rsel),
        "STVT_IFGR": str(ifgr), "STVT_RF": str(rf),
        "STVT_SDR_AGC": "1", "STVT_AGC_SETPOINT": "-20",
        "STVT_EQ": "long", "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1",
        "STVT_DABNOTCH": "0" if rf < 14 else "1",
        "STVT_RS": "erasure", "STVT_RS_ERASURES": "0",
        "STVT_SOVA": "1", "STVT_TURBO": "1", "STVT_EQ_MOD12_GUARD": "1",
        "STVT_SPS": "1.1", "STVT_RRC_SYMS": "8", "STVT_TEISCRUB": "1",
        "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0", "STVT_EQ_TELEM": "1",
        "STVT_IQ_RING": "0", "STVT_PERSIST_RETUNE": "0",
        "STVT_EQ_TAP_CACHE": str(HERE / "lab" / "tapcache"),
    })
    return env


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")   # cp1252 console law
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, required=True)
    ap.add_argument("--ant", default="Antenna B", help="current antenna")
    ap.add_argument("--ants", default="Antenna A,Antenna B,Antenna C")
    ap.add_argument("--rfgain", type=int, default=None)
    ap.add_argument("--ifgr", type=int, default=None)
    ap.add_argument("--secs", type=int, default=45)
    args = ap.parse_args()
    # gain seed: recipe, else the caller, else a safe default
    rec = load_recipe(args.rf)
    rsel = args.rfgain if args.rfgain is not None else \
        (rec or {}).get("rfgain_sel", 3)
    ifgr = args.ifgr if args.ifgr is not None else (rec or {}).get("ifgr", 40)
    try:
        import time_knob as tkn

        def rec_row(ant, m):
            tkn.record({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "rf": args.rf, "ant": ant, "mer": m.get("mer_med"),
                        "loss_pct": m.get("loss_pct"), "source": "deeptune"})

        def hours():
            try:
                return tkn.hint(args.rf, tkn.load())
            except Exception:
                return None
    except ImportError:
        rec_row, hours = None, None

    eng = DeepTune(args.rf, [a.strip() for a in args.ants.split(",")],
                   args.ant, (rsel, ifgr), lambda a, r, i: cli_env(args.rf, a, r, i),
                   HERE / "lab" / "deep_tune_cli.log",
                   status=lambda p, m: print(f"[{p:3d}%] {m}", flush=True),
                   record=rec_row, hour_hint=hours,
                   base_secs=args.secs, race_secs=args.secs)
    res = eng.run()
    print(json.dumps({k: v for k, v in res.items() if k != "grid"},
                     indent=2, default=str))


if __name__ == "__main__":
    main()
