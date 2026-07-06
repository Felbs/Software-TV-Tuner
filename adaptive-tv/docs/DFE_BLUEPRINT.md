# DFE Blueprint — decision-feedback equalization for the TV Tuna
*Drafted 2026-07-06 (Fable's last night) so the next session starts from
a design, not a blank page. Foundation pieces (signed CIR dump, Wiener
solver, race harness) were built and validated tonight.*

## Why a DFE
The linear (FFE-only) equalizer must INVERT the channel: a deep echo
forces huge taps that amplify noise (noise enhancement — the RF15
disease). A DFE instead SUBTRACTS each echo using already-decided
symbols: no inversion, no noise boost. Textbook gain on echo-dominated
channels: 2-4 dB exactly where we are 1-2 dB short.

Patients waiting:
- RF15 rabbit: close-in echoes −13.8 dB @ ±0.2 µs, oscillating 12↔17 dB
- RF9/RF7 canyons on the discone (19-21 dB in-band ripple)
- the universal 34 µs / ~10 km terrain echo seen on every rabbit channel

## Architecture (extend atsc_equalizer_long, keep LMS framework)
```
input ──► FFE (NTAPS=256, existing) ──►(+)──► slicer (8-VSB 8-level)
                                        ▲            │ decided symbol
                                        │            ▼
                                   −(FBF: NFB taps)◄─┘  (feedback FIFO)
```
- FBF length NFB=192 covers echoes to ~18 µs; the 34 µs terrain echo
  needs NFB≈370 — start 192, env `STVT_EQ_DFE_NFB`.
- Slicer: nearest of {±1,±3,±5,±7} (pilot-adjusted ±5.25 offsets as in
  existing trainer); during field sync, feed KNOWN training symbols
  into the FBF instead of decisions (kills error propagation for free
  once per field).
- Adaptation: joint LMS — e = y_ffe − Σ fbf·decided − target;
  FFE: w += µ·e·x (existing volk path); FBF: b += µ_fb·e·d_hist
  (µ_fb ≈ µ/2; decided symbols are exact so FBF converges faster).
- Error propagation guard: freeze FBF adaptation (not application) when
  batch-median |e| > threshold (reuse Huber/EWMA machinery from the
  robust-LMS work — STVT_EQ_ROBUST plumbing exists).

## Seeding (tonight's machinery slots straight in)
- FBF init = measured CIR post-cursor taps (signed, from
  STVT_EQ_CIR_DUMP; sign convention verified via wiener_seed.py
  calibration, axis constant = 55).
- FFE init = Wiener solve of the PRE-cursor + main path only
  (wiener_seed.py --precursor mode: zero h beyond the main peak first).
- Re-seed on quality-reset: where the current code resets taps to
  delta/lkg, optionally reload the analytic seed (env
  STVT_EQ_RESEED=1) — turns dropout recovery from a crawl into a jump.

## Env plan
`STVT_EQ=dfe` (new mode alongside long), `STVT_EQ_DFE_NFB`,
`STVT_EQ_DFE_MU_FB`, `STVT_EQ_RESEED`, telemetry adds `fb|taps|` and
slicer-error-rate to the eq-long line (liveness law: prove decisions
are right, not just training error).

## Validation ladder (use existing instruments)
1. Offline first: tv_replay.py on a captured specimen with STVT_EQ=dfe
   vs long — bit-exact harness already exists (ab harness pattern).
2. race_wiener.py --rf 15: the acceptance test IS the patient. Success
   = headers > 0 on RF15 at its median state.
3. Overnight soak via the cube (STVT_EQ=dfe in chain_env for one night)
   — the 94/94 UHF trio must NOT regress (the flywheel-v2 lesson:
   warmup-gate any new feedback loop).
4. Discone RF9 at dawn window with DFE = the moonshot checkpoint.

## Effort estimate
- C++ core (slicer + FBF + joint LMS + training-symbol feedback): ~150
  lines in atsc_equalizer_long_impl work() — 1 focused session.
- Seeding + reseed hook: ~40 lines + wiener_seed --precursor: half a
  session. Validation: one overnight.

## Known traps (from this project's own history)
- Feedback loops without warmup gates poison downstream (flywheel v1).
- Any new metric needs a liveness denominator (blanker mirage).
- _rebuild.bat does NOT install (cmake --install + xcopy, verify DLL
  mtime).
- A/B on live signal, same channel, back-to-back — replay overfits.
