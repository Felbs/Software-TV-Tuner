# The Any-Antenna Campaign — experiment ladder to the ultimate goal

*Drafted overnight 2026-07-05→06 while the Antenna×Channel×Time cube runs.
Goal: a tuner that decodes TV on WHATEVER is connected — by measuring the
antenna instead of trusting it. Bonus: every lever here has a Radio Tuna
(HD/FM) twin, listed inline.*

## The reframe

"Decode TV on any antenna" decomposes into exactly three capabilities:

1. **Know the antenna** (fingerprint it in 60 s: aperture? multipath?
   overload? polarization? best port?)
2. **Know the instant** (propagation is a function of time — proven by the
   13-cycle overnight; best channel changes by the hour)
3. **Spend the margin wisely** (when MER is 1-3 dB short, the remaining
   levers are DSP: instrumented FEC, DFE, multi-pass rescue)

The cube running tonight attacks #1 and #2 simultaneously and hands-free.

## Ladder (ranked by expected dB-per-effort)

### E1. Antenna×Channel×Time cube  — RUNNING TONIGHT
Both ports wired (B=rabbit ears, A=discone), RSPdx switches in software.
Every 20 min: 6 RFs × 2 antennas × 28 s chain samples → median MER,
seq-headers, verdict; gain self-trim every 3rd cycle; RF7/RF9 ≥15.5
triggers the dawn-tropo voice announce. Deliverable: the empirical map
`(antenna, channel, hour) → MER` = the training set for auto-antenna
selection.
**Radio Tuna twin:** identical cube for FM/HD stations (ports already
parameterized in the panel API; nrsc5 BER is the MER analog).

### E2. Auto-antenna selection (diversity by port switching)
Consume E1's map: when tuning RF n, try the historically-best port first,
fall back to the other on a sub-cliff read. Zero new hardware — the
"antenna swap" the user used to do on a ladder becomes a 50 ms API call.
**Twin:** radio panel picks discone vs rabbit ears per station.

### E3. Antenna fingerprint autoprofile (the 60-second interview)
On "new antenna connected": flatness sweep (in-band ripple → multipath
class), MER-vs-gain ladder (overload class), VHF/UHF split scores
(aperture class), FM-sweep control (is the plumbing alive at all — the
2026-06-20 law). Output: an antenna personality file that seeds gains,
notch settings, and EQ strategy. This is what "any antenna" means
operationally: the tuner interviews the stranger.
**Twin:** Radio Tuna's adaptive survey probe (stepped gains per station —
already an open bug, same code shape).

### E4. Light up the dark 98% (instrument viterbi + RS)
gap_profiler attributed 98% of glitch gaps to NO instrumented stage.
viterbi_metric and RS-fail counts exist as internal stream tags — print
them to stderr (~20 lines in gr-atscplus), re-run the profiler at midday,
get the real disease map. Every later DSP lever is aimed by this.
**Twin:** nrsc5 already prints BER continuously — TV is behind radio here.

### E5. FEC-directed adaptation
Once RS-fail/viterbi metrics stream: use them as the equalizer's outer
cost function (fs_err_rms is the EQ grading its own homework; RS failures
are the ground truth). Slow outer loop: nudge mu / taps / gain to minimize
RS-fails-per-second, not training error. Liveness law satisfied by
construction — the metric IS decoded data health.
**Twin:** servo SDR gain on nrsc5 BER instead of "did it sync".

### E6. DFE seeded by the echo X-ray
The CIR telemetry (STVT_EQ_CIR) already localizes echoes. Feed measured
echo positions/amplitudes as the initial feedback taps of a decision-
feedback section. Target patient: the discone's 19-21 dB RF9 canyons —
if DFE unlocks TV on a DISCONE, "any antenna" is essentially proven.

### E7. IQ multi-pass rescue (the impossible-decode lab)
Record IQ of a 1-2 dB-short channel (rabbit-ears RF7 captures exist).
Decode N passes offline: different EQ seeds, mu schedules, warm-start
taps, PN511_LIMIT settings; vote/splice the TS across passes. Replay
overfits as a *tuning* method (the law) — but as a *rescue* method for a
specific capture it's legitimate: broadcast TS packets are identical
across passes, so any pass's clean packet is truth.
**Twin:** weak HD stations (WPFW at -4.5 dB): capture cu8 once, decode at
many gains/offsets offline.

### E8. Blend philosophy (graceful degradation, the user-facing win)
TV: when RS-fails spike, don't freeze — drop to the strongest sub-program,
or hold last GOP + audio (audio survives 2-3 dB below video).
Radio: HD/analog crossfade driven by BER + audio_probe verdicts (all
ingredients exist as of tonight). "Any antenna" to a human means: the
sound never stops, the picture degrades last.

## Standing laws that govern all of the above
- Liveness denominator: no metric without decoded content beside it.
- Recalibrate forever: every config is stale in hours (cube self-trims).
- Evening scans understate antennas; daylight is the honest window.
- One station/channel reading STATIC/dead? Cross-check another before
  blaming the plumbing (2026-07-05's lesson, four stacked panels deep).
- SDR is single-tenant: bench every other consumer before a campaign.
