# Engineering notes — overnight watch, 2026-07-12

*Written during the overnight autobot run, updated at each check-in.
What it will actually take to make this codebase better, ranked by
(impact × confidence) ÷ effort, grounded in tonight's measurements.*

## 1. The equalizer trilemma — port the NEON int16 EQ (the headliner)

Tonight's data made the trade brutal and explicit on Pi-class hardware:

| EQ | decode quality | Pi real-time? |
|---|---|---|
| `stock` | weak on multipath/marginal | ✅ 2370 KB/s, 0 OsO |
| `long` (float) | strong (the Windows workhorse) | ❌ 1763 KB/s, 14.8 OsO/min *even with fused+minbuf* |
| June `S16 NEON` (pi-port-stvt lineage only) | strong | ✅ (June: 1.21× with fused) |

The S16 NEON equalizer is the only square on the board with both wins.
**What the port takes:** (a) locate the int16 eq block(s) in
`pi-port-stvt`'s gr-atscplus (plus any NEON intrinsics guards); (b) graft
into universal gr-atscplus behind an env switch (`STVT_EQ=long_s16`?)
following the existing block-registry pattern; (c) x86 must still build —
NEON code needs `#ifdef __ARM_NEON` fallback or SSE equivalent; (d)
validation: int16 quantization means bit-compare will NOT hold — the gate
is the replay rail: err_pct within the ±0.14pp noise floor on both
specimens, on both architectures, plus real-time proof on the Pi
(OsO==0 live A/B, per the law). Effort: a day. Impact: "smooth like
cable" extends from strong channels to the whole market on small hardware.

## 2. Player supervision — no more silent mpv deaths

Twice tonight mpv exited and NOTHING noticed: the chain decoded to a file
nobody watched (an appliance that fails silent is a broken appliance).
tv_watch supervises *startup* but not *steady state*; the panel's
chain_doctor watches only the chain. **Fix (small):** extend chain_doctor
— if chain healthy AND panel state says tuned AND mpv absent for >30 s →
fire the same-mux hop (player-only relaunch, machinery already exists).
Add a `player_deaths` counter to the flight recorder so the morning report
shows the failure rate. Also capture mpv's exit: tv_watch should log
`mpv exited rc=N` with its last stderr lines — tonight's deaths left no
corpse to autopsy, which is why the root cause is still unknown.

## 3. Autopilot v2 — race by decode, not by pilot

The pilot race fixed the port-label staleness, but pilot SNR ≠ decode
(RF7: discone hears a +42 pilot it can decode at VHF while a stronger-
pilot UHF antenna fails it; the whole June "pilot saturated at 64 dB"
saga). **Upgrade:** for the top-2 pilot candidates, run a ~9 s MER
early-verdict probe (machinery exists in scan_one_rf — extract it into a
shared helper) and pick by measured MER. Costs ~20 s on a cold tune, only
when candidates are within ~10 dB of each other; cache the verdict in the
channel recipe so repeat tunes skip the race entirely.

## 4. Governor v2 — self-calibration instead of static tiers

Today the governor is `IS_WIN ? full : lean` + hand-set env. The honest
version measures: **first boot, replay a bundled 5-second specimen** (a
~40 MB cs16 in the repo or fetched once), compute ×realtime, and pick the
tier from measured headroom — then re-verify with the OsO==0 gate on
first live tune. That makes ANY new machine (Ubuntu box, next Pi, a
laptop) self-configure with zero folklore. The overnight buffer
experiments (8 MB vs 16 MB vs 4 MB) will also tell us whether
MIN_BUF_BYTES should simply default ON for ARM.

## 5. Label unification — beliefs keyed to antenna identity

The fingerprint ledger knows *which physical antenna* is where; the
belief map / quality history / GAINS / recipes are still keyed by PORT
label (the FOX-tuned-a-dead-port bug, patched by the autopilot's
measurement). The durable fix: stamp every quality_history row and recipe
with the antenna's print-id at write time (antenna_id.rows_for_profile
already reconstructs this retroactively — wiring it in at write time is
~20 lines), and make antenna_for() resolve beliefs through the ledger.
Then the Knob of Time follows the antenna through any port shuffle.

## 6. Cheap wins list

- **Scan wall-time on the Pi**: 493 s vs 140 s for the same market — the
  25 s convergence window × slow Pi lock tests compounds; the MER
  early-verdict should bail dead carriers at ~9 s but the Pi's chain
  startup eats ~15 s before telemetry exists. Pre-warm or shorten.
- **Panel poller budget on small machines**: sparse/cached reads exist,
  but chromium + pollers cost ~100 KB/s of decode rate (browser-off probe:
  1668 vs 1643 median). Kiosk mode should drop poll frequency.
- **tv_watch PID-cache cross-machine**: the (rf,prog)→PID layouts are
  market facts, not machine facts — sync the cache in the repo so a
  fresh box skips the 8-11 s ffprobe discovery per channel.
- **pkill self-match trap**: bit THIRTEEN times across two machines
  tonight. Add `tools/stvt_kill.sh <pattern>` that does the bracket
  transform + separate-invocation dance once, correctly, and use it
  everywhere. Cheap insurance against a proven recurring wound.

## Check-in log

- **00:20** — unit active, FOX soak at 2369 KB/s full rate. mpv died
  again post-retune (pattern #2 confirmed → item 2 above). Notes v1
  written.
- **00:30** — "chain keeps dying (SDRplay crashing)" stage message =
  FOSSIL of the experiment-phase manual kills; service uptime 4h37m
  clean. Doctor lesson: check WHO terminated a process before
  diagnosing (kill-by-hand vs died-alone are different diseases).
- **01:59** — first full rotation done. Experiments: 16MB buffers
  REJECTED (rate dip + oso), 4MB REJECTED (tie), RRC=3 REJECTED (worse
  both axes) — the hand-tuned profile survives three challenges; noise
  discipline behaving. **HEADLINE: NBC RF34 soaked at cc-err 79.2%**
  (near-full rate, 0 oso, other channels 0.05-0.1%) — the stock EQ
  fails THAT channel's multipath specifically. This is item-1/item-4's
  per-channel-EQ case proven by field data: the lean profile needs a
  recipe escape hatch (strong-but-multipath channels get the long EQ
  and eat the overflows, or wait for the S16 NEON port). Rotation
  re-hits NBC ~02:30 — second data point incoming without
  intervention. Also: FOX soak logged cc-err "None" (ts_err returned
  no samples) — measurement bug to check in pi_overnight.py, minor.
- **03:43** — ladder COMPLETE, all five experiments honestly rejected;
  the hand-tuned profile is confirmed optimal on every axis tested.
  **NBC RF34 failure CONFIRMED** (79.2% then 64.7% cc-err, hours
  apart; all other channels 0.014-0.17%) — reproducible, channel-
  specific stock-EQ multipath failure. Morning decision: per-channel
  recipe (long EQ = watchable-but-glitchy 14.8 oso/min) vs wait for
  the S16 NEON port (the real fix). ALSO: FPLL_ALPHA=0.001 — a proven
  WSL RF36 win — HURT the Pi (rate −108, oso +9): third confirmed
  "tuning does not transfer between machines" case (FUSED_GAIN,
  hour-curves, now FPLL α). Machine-local calibration is a LAW —
  governor v2's measured self-calibration (item 4) is the systemic
  answer. Run now in pure soak mode; rig stable all night so far.
- **05:53** — 6 h stable, pure soak. NBC cc-err DECLINING through the
  night: 79.2 → 64.7 → 42.7 → 42.6% — a time-of-day multipath curve
  (evening-worst) surfacing in the data; Knob-of-Time material even
  for a channel the profile can't yet decode. FOX best-of-night
  0.0035%. Rig has needed zero interventions since the ladder ended.
- **08:15 — THE NEON EQ PORT SHIPPED (items 1 + 1b done).**
  `STVT_EQ_S16` cherry-picked from pi-port-stvt b0eadbc (one conflict:
  both lineages added fast paths at the same seam — kept both; our
  lineage's STVT_EQ_FFT overlap-save path discovered in the process).
  x86 regression: packet count exact, TEI within the rail's true
  run-to-run band (two runs of ONE binary differ ±1 TEI — the
  "deterministic rail" is deterministic-within-a-band, not bit-strict).
  Pi replay gate: S16=1 metrics IDENTICAL to float (0.413% / 2119 /
  382800). LIVE NBC RF34: 79%-garbage → **0.000% cc-err, 0 oso, 2367
  KB/s** chain-only; full stack initially re-overloaded via mpv's
  full-res 1080i deinterlace → ported June's **lowdeint** (lowres=1 +
  bwdif, height-cached in the PID cache so launches apply decoder-open
  flags immediately) → **full stack: 2349 KB/s, 0 oso, 0.455%**. Pi
  profile now: fused + fold + minbuf 8MB + LONG EQ + S16 + lowdeint —
  the strong equalizer at real-time on 4 ARM cores. Trilemma closed.
- **CORRECTION to the 03:43 entry:** the overnight FPLL_ALPHA=0.001
  "rejection" was a NULL TEST — the incumbent already ran the chain
  default 0.001, so both arms were identical configs and the observed
  delta was pure measurement noise. The "third non-transfer case" claim
  is RETRACTED (the law still stands on FUSED_GAIN + hour-curves). The
  autobot's conservative accept did the right thing for the wrong
  reason; future experiment ladders must read the INCUMBENT's effective
  value before claiming a comparison.
