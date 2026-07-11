---
name: drizzle-wave-interferer
description: "7/10 glitch saga SOLVED: live DD tracking (STVT_EQ_DD_MU) made the equalizer miss the real-time deadline → SDR source overflows (OsO) → sample splices → all-day glitches. DD evicted from live (base+cliff), offline-only now. LAW: replay CANNOT validate live promotions — overflow-gate them. User right three times"
metadata: 
  node_type: memory
  type: project
  originSessionId: 368b030c-6ab2-4abc-a2a8-33ccdf8dc873
---

**FINAL RESOLUTION (7/10 ~17:45) — supersedes everything below: the
splice SOURCE was LIVE DD TRACKING.** Feature bisect on live air
(direct chains, no readers, fast-disk logs, 150 s arms, RF36):
DD-alone = 54 OsO overflows / 1.02% loss; DD+SOVA = 54 / 1.03%;
full = 52 / 0.77%; SOVA+TURBO-no-DD = 0 / 0.057% GLASS; vintage =
0 / 0.056% GLASS. The equalizer's DD-NLMS misses the real-time
deadline → SoapySDR source drops samples (OsO chars in stderr = the
tell; grep chain logs for "OsO") → phase-step splices → the glitches.
Cable swap + reseats + service restarts all no-ops (correctly — not
hardware). Fix: DD evicted from panel base_env AND cliff env;
offline-only (e7_vote CLIFF_ENV keeps it — no deadline there, wins
real). Full stack after eviction: 0 overflows, 0.054%, MER 19.91
all-time record, turbo rescuing. **LAWS:** (1) offline replay can
NEVER validate a live promotion — replay has no deadline; DD's replay
wall (70 s per 45 s) was the visible tell; gate every live promotion
on OsO-count==0 in a live A/B. (2) DD's live "validation" was masked
by the liveness-denominator law (tested on channels already 6-12%
lossy). (3) User's eyes beat instruments AGAIN (3rd time: datamosh,
RF15, this) — "glitchier than before" was true while replay said
impossible. Morning-window attribution (drizzle 9:06 pre-DD-base)
remains partially unexplained — possibly cliff_mode chains (DD was in
cliff env since 7/09) + service state; do not reopen without new data.

**EVENING RE-DIAGNOSIS (disease_map_20260710.md) — historical, cause
now known (see above):** byte-exact forensics on today's
drizzle capture show pilot PHASE steps (1.1-3.1 rad) with FLAT
amplitude at every burst site, gap lengths off the 313-seg grid, FPLL
railing at π/2 with steady input power → **time removed from the
sample stream = USB splices** (7/04 leaky-cable law; today 35/min).
Even the 7/02 "strong control" capture is splice-contaminated (0.33%).
One ~5 ms splice ≈ 1 field (~300 pkts): breaks EQ grid + viterbi
mod-12 + deint mod-52 at once. AC-off test: no change. Cable reseat
×2: no change (~0.97%). NEXT: different physical cable → different
rear port/controller → SDRplay API update (crash-prone service now
suspect). LAW: gate DSP-comparison specimens on splice-rate≈0.
The "wave ON/OFF" pattern = link intermittency, not an appliance.
Same day: time machine (vintage 2824bde vs new) proved math NOT
broken — new-full ≥ vintage on all 4 specimens; knob audit found
7 conflicts/9 fossils (report: knob_audit_20260710.md, top finding:
DVR/scan path runs a different decoder than live); time-knob v2
SHIPPED (time_knob.py + quality_history.csv, panel wired, cube_map
fossil retired).

**The residual line-glitch (post-datamosh-fix, every 6-10 s during
motion) is NOT software.** 7/10 morning hunt, RF36 Fox:

- Viewing stack 9:06-9:34: continuous drizzle ~0.3-0.5% loss on BOTH
  antennas (rabbit ~200/5s, Philips ~500/5s) + strikes to 2100/5s.
- reg_check ABBA 9:35-9:45 (direct chains, same channel/antenna/SDR):
  all four arms 0.014-0.029% — glass. **IQ ring v2 exonerated**
  (ON arms actually slightly better).
- Full viewing stack relaunched 9:50: also 0.024% — glass. Players,
  panel, extractor all exonerated.
- Only surviving hypothesis: **duty-cycling external interferer**
  (same wave signature as the WETA/RF31 storm 0.06%↔3.5%; AC/
  compressor season). It was ON during the user's glitchy window and
  switched OFF ~9:35.

**Why:** don't bisect code when the fault won't hold still — first
prove stationarity. A drizzle that vanishes mid-investigation convicts
the environment, not the diff.
**How to apply:** `drizzle_watch.py` (adaptive-tv) runs all day, one
log-read/min → `lab/drizzle_watch.csv` (time, bad_pct, mer_med). When
lines reappear on screen, read the CSV: wave edges give the appliance
duty cycle; correlate with household events (AC, fridge, chargers).
Physical hunt = Signal Finder tone + breaker walk. Yesterday's clean
day_lab soaks ran STVT_IQ_RING=0 — red herring; panel runs RING=35 and
it costs nothing.

**Software counterattack (7/10 offline sweep on fox_impulse_storm via
tv_replay):** DD tracking (STVT_EQ_DD_MU=1e-2) is the ONLY arm that
touches the impulse class — −41.6% loss, +75 frames, reproduced 2×.
Entire RS/erasure side inert (GMD-off, erasure budgets 10/18/22,
PN511_LIMIT all within ±0.1% noise): impulse-hit segments arrive
beyond FEC reach; DD closes the equalizer's ~24 ms between-FS blind
window where impulses bite. **DD promoted from cliff-only to
base_env — ALL panel tunes now run it** (tv_tuna_panel.py base_env;
live-safe via warm-up hold v2). Replay CPU ~1.8× on the decode but
live chains ran DD fine on cliff channels for days. Full table:
adaptive-tv lab/impulse_arms_report.md. Residual 1.49% = next lever
(likely viterbi/segment-sync burst overrun — instrument before acting).

Related: [[datamosh-presentation-layer]], [[day-quality-baseline]],
[[dont-hammer-chain-cpu]]
