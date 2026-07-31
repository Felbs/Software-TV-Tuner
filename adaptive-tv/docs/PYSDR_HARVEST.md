# PYSDR_HARVEST.md — full-book mining, 2026-07-10

All 26 chapters of pysdr.org read (4 parallel readers briefed on our
stack + measured open problems). 23 applicable techniques. The TDOA
chapter alone had already yielded GCC-PHAT (validated instrument);
this is the rest, ranked and cross-referenced against our findings.

## A. THE BREATHING-CHANNEL OFFENSIVE (attacks the #1 enemy)
RF9-class disease, per our data: loss 6%→60% by hour, median MER
blind to it, NO static echo (echo_phat verdict) — dynamic fading
faster than the 24 ms field-sync rate.

1. **Decision-directed equalizer tracking** — adapt per-symbol on
   sliced decisions between field syncs (μ_DD 10-100× smaller than
   training μ), gated by instantaneous MER (freeze below ~17 dB so
   error bursts can't corrupt taps — honors the liveness law).
   Attacks the 41 Hz adaptation bottleneck head-on. Intermediate:
   light retrain on the 4-symbol segment sync at 13 kHz.
   THE biggest single lever on breathers. [sync ch.]
2. **Time-based erasure marking through the deinterleaver** — flag
   bytes by WHEN they crossed the air (during a measured fade/impulse)
   and trace through the interleaver map into the RS/GMD erasure
   ladder. This is sickmap v2 with the correct signal: v1 marked by
   observed corrections (wash); marking by fade-time is what the
   physics supports. Pure bookkeeping, stacks with everything.
   [channel coding framing + our own sickmap plumbing]
3. **Coherence-time measurement** — autocorrelate the dominant
   GCC-PHAT tap's complex gain across successive field syncs →
   Doppler spread / coherence time of the breathing. One evening of
   logging; DECIDES whether DD-tracking (1) suffices or only
   diversity wins. Run FIRST. [multipath ch.]
4. **Clarke-model synthetic fader for tv_replay** — sum-of-sinusoids
   Rayleigh fading stage (parameterized by measured f_D from (3))
   multiplied onto clean specimens → breathing channels ON DEMAND in
   the replay lab. Kills our slowest experimental loop (waiting for
   RF9's dawn window). [multipath ch.]
5. **Selection diversity TODAY (zero hardware)** — MER-dial-driven
   auto antenna switching on the existing 3-port switch when the
   current branch enters a canyon. The cube's "best channel is a
   function of time" already proves the branches decorrelate.
   [doa ch. degenerate case]
6. **Impulse flags → erasures (CFAR + variance spike)** — CA-CFAR
   adaptive threshold (fixes WHY the old blanker died: fixed threshold
   on a moving floor) or 1-MAC/sample IIR power tracker; FLAG samples
   (not blank), map through group delay into (2)'s erasure channel.
   [detection + noise chs.]

## B. THE ENABLER
7. **Overlap-save FFT convolution** for RRC + the 256-tap EQ filtering
   path (block-LMS one step away) — ~10× MAC reduction at our tap
   counts. This is the CPU budget that makes (1)+(6)+DFE-everywhere
   affordable in real time. [filters ch.]

## C. DIAGNOSTICS
8. **Spectrogram gap flight-recorder** — 250 ms ring of contiguous
   1024-pt |FFT|² rows (128 µs/slice), frozen+dumped beside the IQ
   specimen on every gap: shows whether the fade sweeps in frequency,
   collapses broadband, or is impulse-shaped. Feeds the failure-class
   taxonomy per-gap. [frequency domain ch.]
9. **Welch averaging + real window for the flatness meter** — average
   ~32 overlapped windowed periodograms; ripple becomes a stable
   number (single-FFT bins scatter ±several dB). [freq domain ch.]
10. **Cyclostationary sub-cliff detector** — FSM at known α (symbol
    rate): "ATSC energy present, X dB below decode" for aiming and
    scan verdicts on channels too weak to equalize. [cyclo ch.]
11. **DC-spur offset tuning** — small LO offset + digital re-shift
    (fold into FPLL NCO like FPLL_FOLD) evicts the zero-IF spur from
    mid-channel; frees EQ taps, cleans the flatness meter. Mind the
    8 MS/s guard-band margin. [sampling ch.]
12. **Segmented non-coherent PN511 correlation** — fallback FS search
    that survives deep fades/carrier wobble (~1-2 dB integration loss
    for fade immunity), used only when the coherent peak drops.
    [detection ch.]

## D. OPEN-SOURCE / UX (the "anyone anywhere" goal)
13. **SigMF for the specimen library** — our .cs16 IS already valid
    SigMF data (ci16_le). Migrator maps sidecars → core fields +
    stvt: extension namespace; ANNOTATIONS mark pathology sample
    ranges (gap_storm, mer_canyon) so tv_replay/e7_vote seek straight
    to the disease; .sigmf tar archives + IQEngine = shareable
    specimens from any user's market. Cheapest big win in the list.
    [iq_files ch.]
14. **Link-budget calculator** — "why can't I get this tower?" button:
    FSPL(d,f) + ERP vs the constant −106.4 dBm/6 MHz noise floor vs
    our own measured 15.2 dB cliff → verdict in honest dB ("physics
    says no" vs "you should get this — suspect antenna"). Okumura-Hata
    pessimistic second line. [link budgets ch.]
15. **Antenna figure-of-merit** — measured-vs-predicted delta across
    FM beacons = calibrated (Gr − losses) per antenna chain, one
    comparable dB number; feeds (14) with real gain. [link budgets]
16. **SSE push telemetry** — replace panel HTTP polling with one
    Server-Sent-Events stream (~20 lines, no libs); EMA-smooth PSD
    server-side; send only new waterfall rows (~50× payload cut).
    [pyqt ch. pattern]
17. **RDS decode for beacon identity** — PI code + station name
    verifies each FM beacon and gives tower coords via FCC lookup →
    labeled, azimuth-known path sounders for LOCAL_DISCOVERY. ~200
    lines; the chapter's sync-state machine is copy-worthy. [rds ch.]

## E. TWO-COHERENT-TUNER FUTURE (diversity endgame)
18. **⚠ HARDWARE WARNING FIRST**: RSPduo dual-tuner COHERENT mode is
    ~2 MHz per tuner — CANNOT carry a 6 MHz ATSC channel. Full-band
    coherent diversity needs an AD9361-class 2-RX device (PlutoSDR
    rev C/D with 2nd RX enabled, USRP B210). VERIFY SPECS BEFORE
    BUYING. (Pilot-band interferometry/monopulse still works on duo.)
19. **LMS combining trained on field sync = self-calibrating MRC** —
    2-tap spatial LMS with PN511 as reference absorbs inter-tuner
    phase/gain offsets automatically (no explicit cal for combining).
    Roadmap: EGC on pilots (1 afternoon, +3 dB) → LMS-MRC (diversity
    gain ~8-10 dB at deep-fade percentiles — the real breathing cure)
    → space-time equalizer (each EQ tap a 2-vector). [doa ch.]
20. **2×2 MVDR null steering** — closed-form 2×2 R⁻¹; one steerable
    null for dynamic co-channel echo; update R at the measured
    breathing rate. [doa ch.]
21. **Monopulse Δ/Σ aiming** — signed left/right error → stereo-pan
    audio aiming; close-spaced pair (d≤λ/2) on a boom; also absolute
    tower bearings from pilot-phase interferometry. [phaser ch.]
22. **Calibration doctrine** — common-source cal per session (cal
    drifts run-to-run = our "recalibrate forever" law), kill AGC
    during cal; eigenvector-of-R trick when one dominant signal
    present. [phaser + 2d chs.]
23. **Wide-spacing truth** — meters-apart antennas = grating-lobe
    soup: never "beam-steer" them; wide spacing is BETTER for
    diversity (decorrelated fading). Plan accordingly. [doa ch.]

## Nothing-new chapters
Intro, Digital Modulation, Pulse Shaping, RTL-SDR, HackRF, FPV,
About; Channel Coding (intro-level vs our SOVA/GMD stack).

## Suggested sequencing vs the current build list
User's standing order: auto-E7 → echo X-ray panel integration →
turbo 2b. The harvest slots in as: (3) coherence-time is a FREE
overnight logger — run tonight alongside anything; (5) auto antenna
switching and (13) SigMF are small standalone wins; (1)+(7) are the
next big decoder builds after turbo 2b (or instead of it — (1) is
likely worth more than 2b on breathers, since our RS-layer analysis
showed the bimodal wall); (4) makes every future decoder A/B faster.
