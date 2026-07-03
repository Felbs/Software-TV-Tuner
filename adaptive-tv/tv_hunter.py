"""tv_hunter.py — adaptive sentinel: always deliver the best watchable TV
the conditions allow, and log the floor while doing it.

Every cycle it fights, not waits:
  - surveys the decodable channels (impulse load is channel-specific and
    MOVES: RF34 was cleanest at night, RF31 in the morning, RF36 midday)
  - camps the quietest mux, plays the ROBUST program tier for the floor
    (HD when clean, SD when the floor is high — SD's thinner packet
    stream shrugs off bursts that wound HD)
  - supervises playback like tv_up (playhead-verified, stall bounce,
    rotation) and re-surveys every 30 min or on hard degradation
  - appends every score to lab/floor_history.jsonl — the dataset for a
    future time-of-day floor forecast
  - when any channel's floor supports HD cable (gaps/min <= 3, 20 min),
    switches to HD there and logs CABLE WINDOW OPEN

Log tokens: HUNT / CAMP / HOPPED / TIER / CABLE WINDOW OPEN / BOUNCE / FATAL
"""
import json, os, statistics, subprocess, sys, time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tv_lab import (ts_metrics, build_env, start_chain, kill_chain,
                    kill_players, wait_decode, log, mpv_get, LIVE, PY,
                    ROTATE_BYTES)

PLAYER = str(Path(__file__).resolve().parent / "play_marginal.py")
HIST = Path(__file__).resolve().parent / "lab" / "floor_history.jsonl"

# program map per mux: (HD program, robust SD program)
CHANNELS = {36: (3, 5), 31: (1, 2), 34: (3, 3)}
SD_TIER_GAPS = 8.0        # floor above this: play SD tier
CABLE_GAPS, CABLE_HDRS, HOLD_S = 3.0, 4.5, 20 * 60
SURVEY_EVERY = 30 * 60

ARGS = SimpleNamespace(rf=36, program=3, antenna="Antenna A",
                       ifgr=32, rfgain=2)


def survey():
    """~50 s per channel: camp briefly, measure gaps + headers."""
    results = {}
    for rf in CHANNELS:
        kill_chain(); time.sleep(2)
        env = build_env(ARGS, 0)
        ch = start_chain(rf, env)
        time.sleep(55)
        m = ts_metrics(35)
        ch.terminate()
        try: ch.wait(timeout=6)
        except Exception: ch.kill()
        if m and m["hdrs_s"] >= 2 and m["real_pct"] > 30:
            results[rf] = m
            log(f"  RF{rf}: gaps/min={m['gaps_min']:.1f} "
                f"hdrs/s={m['hdrs_s']:.1f}")
        else:
            log(f"  RF{rf}: not decodable now")
    return results


def start_player(prog, env):
    logf = open(Path(os.environ["TEMP"]) / "tv_hunter_player.log", "w")
    return subprocess.Popen([PY, "-u", PLAYER, str(prog), "--tail-mb", "15",
                             "--strong", "--cc", "--cache-secs", "12"],
                            env=env, stdout=logf, stderr=subprocess.STDOUT)


def playing():
    a = mpv_get("time-pos")
    if a is None: return False
    time.sleep(3)
    b = mpv_get("time-pos")
    return a is not None and b is not None and b > a + 0.5


def record(rf, prog, m):
    with open(HIST, "a") as f:
        f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M"),
                            "rf": rf, "prog": prog,
                            "gaps_min": round(m["gaps_min"], 1),
                            "hdrs_s": round(m["hdrs_s"], 1)}) + "\n")


def main():
    kill_players(); kill_chain(); time.sleep(2)
    log("HUNT initial survey")
    res = survey()
    if not res:
        log("FATAL nothing decodable"); sys.exit(1)
    rf = min(res, key=lambda r: res[r]["gaps_min"])
    floor = res[rf]["gaps_min"]
    hd, sd = CHANNELS[rf]
    prog = hd if floor <= SD_TIER_GAPS else sd
    tier = "HD" if prog == hd else "SD"
    log(f"CAMP RF{rf} ({tier}, prog {prog}) floor={floor:.1f} gaps/min")

    kill_chain(); time.sleep(2)
    env = build_env(ARGS, 0)
    chain = start_chain(rf, env)
    if not wait_decode():
        log("FATAL camp chain no video"); sys.exit(1)
    player = start_player(prog, env)
    time.sleep(15)
    log("TIER %s playing %s" % (tier, "(verified)" if playing() else
                                "(verify pending)"))
    hold = None
    last_survey = time.time()
    recent = []
    while True:
        time.sleep(60)
        if chain.poll() is not None:
            log("BOUNCE chain died")
            chain = start_chain(rf, env); wait_decode()
            kill_players(); player = start_player(prog, env); time.sleep(12)
            continue
        if LIVE.exists() and LIVE.stat().st_size > ROTATE_BYTES:
            log("rotate 6GB")
            kill_players(); chain.terminate()
            try: chain.wait(timeout=6)
            except Exception: chain.kill()
            time.sleep(2)
            chain = start_chain(rf, env); wait_decode()
            player = start_player(prog, env); time.sleep(12)
            continue
        if not playing():
            log("BOUNCE player stalled")
            kill_players(); time.sleep(1)
            player = start_player(prog, env); time.sleep(12)
        m = ts_metrics(60)
        if not m: continue
        record(rf, prog, m)
        recent.append(m["gaps_min"]); recent[:-15] = []
        cable_ok = m["gaps_min"] <= CABLE_GAPS and m["hdrs_s"] >= CABLE_HDRS
        log(f"SCORE RF{rf}/{tier} gaps/min={m['gaps_min']:.1f} "
            f"hdrs/s={m['hdrs_s']:.1f} {'OK' if cable_ok else '--'}"
            + (f" hold={int(time.time()-hold)}s" if cable_ok and hold else ""))
        if cable_ok:
            hold = hold or time.time()
            if time.time() - hold >= HOLD_S:
                if tier != "HD":
                    log("floor supports cable — switching to HD tier")
                    prog, tier = hd, "HD"
                    kill_players(); player = start_player(prog, env)
                    time.sleep(12)
                log("CABLE WINDOW OPEN — HD cable criterion held 20 min")
                hold = None
        else:
            hold = None
        # tier adaptation within the camp
        med = statistics.median(recent[-8:]) if len(recent) >= 8 else None
        if med is not None:
            if tier == "HD" and med > SD_TIER_GAPS * 1.5:
                log(f"TIER HD->SD (floor {med:.0f})")
                prog, tier = sd, "SD"
                kill_players(); player = start_player(prog, env); time.sleep(12)
                recent.clear()
            elif tier == "SD" and med <= SD_TIER_GAPS * 0.6:
                log(f"TIER SD->HD (floor {med:.0f})")
                prog, tier = hd, "HD"
                kill_players(); player = start_player(prog, env); time.sleep(12)
                recent.clear()
        # re-survey: periodic or hard degradation
        degraded = med is not None and med > 45
        if time.time() - last_survey > SURVEY_EVERY or degraded:
            log(f"HUNT re-survey ({'degraded' if degraded else 'periodic'})")
            kill_players(); chain.terminate()
            try: chain.wait(timeout=6)
            except Exception: chain.kill()
            res = survey()
            last_survey = time.time()
            if res:
                new_rf = min(res, key=lambda r: res[r]["gaps_min"])
                floor = res[new_rf]["gaps_min"]
                if new_rf != rf:
                    log(f"HOPPED RF{rf} -> RF{new_rf} (floor {floor:.1f})")
                rf = new_rf
                hd, sd = CHANNELS[rf]
                prog = hd if floor <= SD_TIER_GAPS else sd
                tier = "HD" if prog == hd else "SD"
            kill_chain(); time.sleep(2)
            chain = start_chain(rf, env); wait_decode()
            player = start_player(prog, env); time.sleep(12)
            recent.clear(); hold = None


if __name__ == "__main__":
    main()
