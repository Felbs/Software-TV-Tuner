# WL v3 — adaptive conjugate shrinkage + the dual-decode A/B harness (2026-07-29)

Branch `stvt-2.0-wl`, local only. Replay/offline throughout: the all-day TV
ladder (`lab/day_program_729.py`) held the radio, so no SDR was touched. The
only live-adjacent action was installing a freshly built `gnuradio-atscplus.dll`
between the ladder's cycles.

Two deliverables:

1. **`tools/tv_dual.py`** — a DUAL-DECODE harness: one input stream, two
   equalizers, two transport streams, sample-aligned. The instrument.
2. **WL v3 adaptive conjugate shrinkage** in `atsc_equalizer_wl` — make the
   conjugate branch earn its weight, measured per field sync, opt-in.

---

## 1. The instrument: `tools/tv_dual.py`

Every WL-vs-long comparison until today was two separate runs — two slices of a
fading channel live, or two independent adaptation trajectories offline. The
difference being argued about (a few percent) was smaller than the variance of
the measurement. `tv_dual` removes the variance entirely:

```
file -> resamp -> matched filter -> fpll(FOLD) -> atsc_wl_frontend
                                                    |  out0 REAL segments
                                                    |  out1 plinfo
                                                    |  out2 IMAG segments
              +-------------------------------------+
              |                                     |
   atsc_equalizer_long (0,1)            atsc_equalizer_wl (0,1,2)
              |                                     |
   viterbi/dei/RS/derand/depad/TEI      viterbi/dei/RS/derand/depad/TEI
              |                                     |
        <tag>_long.ts                         <tag>_wl.ts
```

Both equalizers consume the SAME segments with the SAME plinfo. Any difference
between the two TS files is the equalizer and nothing else. AWGN (for cliff
sweeps) is injected upstream of the tee, so both legs see bit-identical noise.

### Harness fidelity — proven, not asserted
| stream | md5 |
|---|---|
| `tv_replay.py STVT_EQ=long` (production v1 path), rf34_ctrl | `F1F867C5567B33721684F4FBF7C423BB` |
| `tv_dual.py` **long leg**, same capture | `F1F867C5567B33721684F4FBF7C423BB` |
| `tv_dual.py` **wl leg**, same capture | `AF9769A6F60C2BEBF6C6A50CF7CD8440` |

The long leg is byte-for-byte the production decode, and the wl leg reproduces
the hash recorded in `lab/wl_fused/WORKLOG.md` on 7/27. The harness is not an
approximation of the two chains — it *is* both chains, running at once.

Cost: 17.9 s wall for a 15 s capture with BOTH backends (one shared front end).

### What it measures
- delivered VIDEO frames per leg (ffmpeg null-sink `-map 0:v -stats`; ffprobe
  lies on multi-program TS)
- per-field-sync MER for both equalizers, PAIRED (same field, same symbols):
  p5 / p10 / p50 / p90 — low-tail percentiles, because fast fades live in the
  tail and the median lies about them
- WL-advantage counters: fields where WL's error is lower / higher / tied
- conj_frac and imag_frac vs MER scatter (where WL earns its keep)
- delivered-frames-per-MER-bin (the watchability curve input)
- `--diag-dir`: dumps both equalizers' output planes, and the scorer then
  computes an UNBIASED slicer (decision-directed) MER with an identical
  definition for both legs — see the caveat below
- everything lands in `<tag>.json` for later charting

### Caveat found while building it (important)
`fs_err_rms` is each equalizer's own TRAINING error, measured on the field sync
*while adapting*, and the two blocks adapt differently (`long`: fixed-beta LMS,
beta=5e-5; `wl`: NLMS, mu=0.5). WL's number is therefore optimistically biased —
it partially fits the training field. The two MER series are comparable to each
other only in *shape/trend*, not in absolute dB. The slicer MER computed from
the output dumps is the apples-to-apples number, and delivered frames is the
product metric. Do not quote "WL MER is +2.4 dB" from the raw telemetry.

---

## 2. WL v3: adaptive conjugate shrinkage

### The algebra first (this changed the design)
Reading the folded filter and its update rule end to end:

```
y = Re( sum w1 x + w2 conj x ) = dot(xr, a) + dot(xi, b)
a = Re w1 + Re w2      b = Im w2 - Im w1
```
and the WL-NLMS update (`w1 += s conj x`, `w2 += s x`, s real) gives

```
Re w1 += s xr,  Re w2 += s xr    =>  (Re w1 - Re w2) NEVER changes
Im w1 -= s xi,  Im w2 += s xi    =>  (Im w1 + Im w2) NEVER changes
```

So the two "extra" degrees of freedom of the augmented filter are **frozen at
their init values** and live entirely in the null space of the real output. The
block is *exactly* an NLMS over the doubled real regressor `[xr; xi]` with
coefficients `[a; b]`.

Three consequences:

1. **`conj_frac` (|w2|²/(|w1|²+|w2|²)) is a poor telemetry.** Because
   `Re w1 - Re w2 = delta` is frozen, conj_frac mostly reports how far the taps
   have moved from the delta init, not what the conjugate branch contributes.
   The honest metric is **`imag_frac` = |b|²/(|a|²+|b|²)** — the share of the
   filter that the production linear equalizer does not have. (Measured on
   clean rf34: conj_frac 0.11 vs imag_frac 0.21.)
2. **Shrinking `w2` — the literal reading of "shrink the conjugate taps" — is
   the wrong lever in this architecture.** It only takes `b` to `(1-k/2)·b`
   (never to zero) *and* corrupts the shared linear part: `a -> a - k·Re w2`.
   Proven in `wl_degenerate_test.py` T3.
3. **The right lever is the imag-plane vector `b`** — the entire extra degree of
   freedom WL has over `atsc_equalizer_long`. Scaling `Im w1` and `Im w2`
   together scales `b` exactly by the same factor, leaves `a` bitwise unchanged,
   and preserves the `Im w1 = -Im w2` invariant.

### The control law
Per field sync, BEFORE any update (so the coefficients being judged were fitted
on the *previous* field — a genuine generalization test):

```
e_lin   = sum (d - dot(xr, as))^2                  shadow's linear part only
e_probe = sum (d - dot(xr, as) - dot(xi, bs))^2    + the shadow's imag plane
B       = max(0, (e_lin - e_probe) / e_lin)        "the conjugate branch earns B"
kappa   = kappa_max * exp(-B / B0)                 (held off for WARMUP fields)
```
`kappa` is then applied as a leak on the imag plane after every training symbol
(`leak = (1-kappa)^(1/704)`), so B ~ 0 (conjugate branch worth nothing) drives
`b -> 0` and WL degenerates to the strictly-linear real equalizer; B >> B0
(impropriety real) releases the shrinkage.

**`(as, bs)` is a COUNTERFACTUAL SHADOW EQUALIZER** — a complete, never-shrunk
widely-linear filter in the folded domain, adapted on the same field syncs with
the same NLMS, whose output goes nowhere. It only runs on field-sync symbols
(704 of every 313x832), so it costs ~0.02% of the filter load. It exists
because the controller must not be able to suppress its own evidence. Two
cheaper designs were built and both LOCKED OUT, measured:

| probe design | what happened |
|---|---|
| score the LIVE taps | shrinkage drives `b -> 0`, `B` reads 0 forever, kappa pinned at max. **WL 228 -> 0 frames at the cliff knee.** |
| shadow of the imag plane only, driven by the live error | slower lock-out: once `b` is suppressed the live `a` absorbs the residual and the shadow stops seeing anything. **rf9: kappa 0.969, WL 350 -> 290 frames.** |
| full counterfactual shadow equalizer (shipped) | rf9 kappa 0.003, 348 frames — the branch keeps its weight. |

A 16-field warm-up hold keeps the controller from judging an untrained branch
(same discipline as the long equalizer's `d_fs_trained>=3` hold).

### In-sample vs out-of-sample (the trap that redesigned this)
The first implementation measured the benefit POST-adaptation on the same field
sync the taps had just been fitted to. It read **B = 0.94 on a clean strong
channel** — i.e. "the conjugate branch removes 94% of the error", which would
have justified never shrinking anything. It is overfitting: 256 real
coefficients fitted to 704 training symbols will always explain the training
field. Both numbers are now emitted (`ben` = out-of-sample, steers kappa;
`beni` = in-sample, diagnostic) and the gap between them IS the estimation
variance the WL theory predicts.

### Knobs (all opt-in; v2 behaviour is the default)
| env | default | meaning |
|---|---|---|
| `STVT_WL_SHRINK` | 0 | 1 = enable v3 shrinkage |
| `STVT_WL_SHRINK_GAIN` | 0.5 | kappa_max (per-field shrink at zero benefit) |
| `STVT_WL_SHRINK_B0` | 0.02 | benefit scale in the exponential |
| `STVT_WL_SHRINK_FORCE` | 0 | 1 = kappa := GAIN always (no measurement) — the degenerate-to-linear test arm |
| `STVT_WL_SHRINK_WARMUP` | 16 | field syncs before shrinkage may engage |
| `STVT_EQ_TELEM_EVERY` | 8 | telemetry cadence, both equalizers (1 = every field, for paired A/B) |

### The degenerate-to-linear proof (`lab/wl_v3/wl_degenerate_test.py`)
12/12 checks pass in float64, on synthetic improper data (and optionally on a
real capture dump):

- T1 folding identity, max rel err 2.1e-14
- T2 `Re w1 - Re w2` and `Im w1 + Im w2` frozen (drift 8.9e-16 / 0.0); folded
  update == NLMS on `[xr; xi]` (deviation 1.1e-16)
- T3 shrink-w2 gives `b -> (1-k/2) b` and corrupts `a` (|da| 0.185); v3
  imag-plane shrink gives `b -> (1-k) b` EXACTLY with `a` bitwise unchanged
- T4 at kappa=1: `b == 0` at every evaluation (max |b| = 0, exact); the folded
  filter output is **bitwise** the real-only dot product (the b term contributes
  exactly +0.0); and the resulting tap trajectory tracks an independently
  written real-only NLMS to 1.1e-16 (the residual 1.8e-14 on y is complex-dot
  vs real-dot summation order, nothing else).

Honest scope: WL at kappa=1 degenerates to *a* strictly-linear real FFE — the
same filter family as `atsc_equalizer_long`, not the same block. It cannot be
bit-identical to `atsc_equalizer_long`, which has 256 taps (not 128), fixed-beta
LMS (not NLMS), a leak, LKG snapshot/restore, DFE and MOD-12 machinery. The
claim proven is structural degeneracy, not binary equality.

---

## 3. Results — the first sample-aligned WL-vs-long table ever taken

All rows: `lab/wl_v3/sweep.py`, one input stream, both equalizers, arsenal env
(FOLD/soft-viterbi/erasure-RS/SOVA). Frames = ffmpeg null-sink video frames.
`degen` = `STVT_WL_SHRINK_FORCE=1 GAIN=1.0` = WL forced to its own
strictly-linear reduction (imag plane identically zero).

| capture | long | WL v2 | WL v3 (g10) | WL degen | imag_frac | benefit B (out-of-sample) | kappa |
|---|---:|---:|---:|---:|---:|---:|---:|
| rf34 clean (control)      | 403 | 403 | 403 | 403 | 0.206 | 0.932 | 0.000 |
| rf34 + AWGN 2147 (knee)   | 131 | 226 | **230** | **0** | 0.120 | 0.597 | 0.000 |
| rf7 marginal (real)       | 250 | 257 | 257 | 178 | 0.146 | 0.589 | 0.000 |
| rf9 marginal (real)       | 113 | 350 | 348 | **0** | 0.119 | 0.634 | 0.003 |
| rf35 marginal (real)      | 396 | 396 | 396 | 396 | 0.137 | 0.902 | 0.000 |
| rf27 (never decodes)      |   0 |   0 |   0 |   0 | 0.124 | 0.226 | 0.000 |

### What this says

1. **The strong-channel WL deficit does not exist.** On the clean control WL and
   long deliver the *same 403 frames* — an exact tie, not "WL is 1-2% behind".
   The historical deficit (7/29's live FOX 5377 vs 5800 = 93%) was two separate
   live runs of a fading channel; under sample-aligned measurement it vanishes.
   That is precisely the variance the harness was built to remove.
2. **The WL cliff advantage is large and reproduces**: +73% at the AWGN knee,
   **+210% on rf9**, +3% on rf7, tie on the two easy captures. WL is never worse.
3. **The imaginary plane is doing all of it.** The `degen` column is WL with the
   conjugate branch forced off: rf34-knee 226 -> 0, rf9 350 -> 0, rf7 257 -> 178.
   Turning the imag plane off does not return WL to `long` — it returns it to
   something *worse* than long, because WL's real-plane filter is 128-tap NLMS
   while `long` is 256-tap fixed-beta LMS. So the honest decomposition is: WL's
   linear half is WEAKER than the production equalizer, and its imaginary half
   more than makes up the difference. There is a straightforward improvement
   sitting in that sentence (give WL long's real-plane machinery).
4. **The "high SNR = conjugate branch is dead weight" premise is REFUTED for
   this chain.** The out-of-sample benefit B is 0.93 on the *clean* capture —
   higher than at the cliff (0.60). Post-FPLL, the imaginary companion is not
   noise, it is the vestigial sideband, and it predicts the field sync better
   the *cleaner* the channel is. Consequently the v3 controller correctly
   chooses kappa ~ 0 everywhere and **v3 == v2 in delivered frames**. v3's value
   here is the measurement and the safety net, not a frame gain.
5. **rf27 diagnosis (task #37 fallout):** MER p10 at the equalizer input is
   **5.2-5.6 dB**, ~10 dB below the 15.2 cliff, identical for both equalizers.
   RF27's problem is not the equalizer and not the antenna — nothing decodable
   is reaching the equalizer in that capture.

### The finding that surprised me most
At the AWGN knee, with the diag taps on, both equalizers were scored with the
SAME slicer (decision-directed) MER definition on their delivered segments:

| leg | slicer MER p5 | p10 | p50 | p90 | field-sync MER (paired) | frames |
|---|---:|---:|---:|---:|---:|---:|
| long | 16.96 | 17.06 | 17.41 | 17.73 | — | 132 |
| wl   | 16.84 | 16.95 | 17.31 | 17.64 | -0.197 dB vs long | 228 |

**WL's MER is very slightly WORSE on both metrics, and it delivers 73% more
video.** Both TS files are the same size (36.3 MB); the difference is *which*
packets are corrupt, not how many symbol errors there are. So MER — the dial
this lab steers by — is blind to the mechanism by which WL wins. The advantage
must live in the error *structure* (burst length / distribution across the
RS interleaver), not the error *rate*. That is the next investigation, and it
is only visible because the two legs were measured on identical samples.

### Measurement noise floor (5 identical-input repeats)
- `long` leg: 129 / 131 / 132 / 134 / 134 frames at the knee (spread 5, ~+-2%)
- `wl` leg:   226 / 228 / 228 / 228 / 228 (spread 2, ~+-0.9%)
- clean capture: 403 / 403 for both, every run

The `long` leg is NOT bit-reproducible across processes: its TS md5 takes at
least two values (`F1F867C5...`, `AA0DB81B...`, `3D8C11EE...`) for identical
input. Cause: `filterN` calls `volk_32f_x2_dot_prod_32f` whose dispatcher picks
the aligned or unaligned kernel from the runtime pointer alignment, and the
block's `data_mem`/`d_taps` land at different alignments in different processes
-> different summation order -> 4th-decimal differences in `fs_err_rms` that
compound. Verified: the divergence starts at field 26 as a 1e-4 wobble, with
identical framing (620 fields both runs) and no LKG/sheriff/reset events (all
those knobs are off by default). **Standalone `tv_replay` in the same process
shape reproduces exactly** — which is why the historical md5 gate has always
held — but any conclusion about `long` drawn from a +-2-frame difference across
processes is inside the noise. WL is not affected (its taps/window happen to
land stably), which is luck, not design.

> **CORRECTION 2026-07-29 (evening) — "WL is not affected" is WRONG, and the
> single-run hash gate is INVALID for both equalizers.** 15 identical WL runs
> gave three distinct TS md5s (`AF9769A6...` x12, `D8B4F370...` x2,
> `55EB2FAA...`); a 40-run repeat added `BF5FFB10...`, and the `long` path has
> now shown a fourth (`92D014CD...`) as well. The known hash SET is open-ended
> for both. The law and the valid replacement gate now live in
> **`lab/gate_lib.py`** (multi-run modal hash + frame median/spread), and this
> file's own control test was rebuilt on it — see §6 below. Also measured: at the
> AWGN knee the `long` leg's FRAME COUNT is not reproducible to +-2 either
> (131 / 132 / 133 over three identical runs), so cliff comparisons need a
> wider tolerance than clean ones.
>
> One thing DID become bit-reproducible: `VOLK_GENERIC=1` (volk 3.2.0) forces the
> plain-C kernels, and 3/3 runs match exactly — `long` -> `F1F867C5...` (the
> documented modal hash), `wl` -> `D8B4F370...`. Cost 1.6-1.8x wall time, so it
> is a TEST-ONLY bisecting knob (`gate_lib.DETERMINISTIC_ENV`), never a default.

## 4. Gates

- **Default path bit-identity**: `tv_replay STVT_EQ` unset/long on rf34_ctrl ->
  `F1F867C5567B33721684F4FBF7C423BB` before and after every C++ change. PASS.
- **WL v2 bit-identity** (shrinkage off must be exactly v2):
  `AF9769A6F60C2BEBF6C6A50CF7CD8440` before and after. PASS.
- **v3 keeps the marginal advantage**: at the knee v3 230 vs v2 226 frames
  (v2 noise band 226-228); rf9 348 vs 350; rf7 257 vs 257. PASS.
- **v3 matches long on clean captures**: 403 = 403 = 403. PASS.
- **Degenerate-to-linear**: `wl_degenerate_test.py` 12/12; and in the live C++
  block the `degen` arm reports `imag_frac` exactly 0.0000 with the WL MER
  falling onto long's (clean capture: WL p10 21.15 -> 18.79 vs long 18.96).
- Build: `_rebuild.bat` then `_install.bat` (new — `_rebuild.bat` does NOT
  install; Python otherwise keeps importing the stale module). Installed
  between the ladder's cycles; the ladder holds the DLL while decoding and
  `cmake --install` fails cleanly (no partial write) if you try mid-cycle.

## 5. Honest gaps / next

- **The controller has never had a reason to fire.** kappa ~ 0 on all six
  captures, so the shrinkage path is exercised only by the FORCE arm. It is
  correct-by-measurement but unproven in anger. It stays opt-in.
- **The frames-vs-MER disconnect is unexplained** (see above). Error-burst
  structure is the hypothesis; the instrument to test it now exists (dump both
  legs' RS input with `--diag-dir` and compare burst-length histograms).
- **WL's real plane is the weak half** (128-tap NLMS vs long's 256-tap LMS).
  Porting long's real-plane machinery into the WL block is the obvious next
  gain, and it is a bigger lever than shrinkage.
- **No live validation.** Everything here is replay. Any promotion still owes
  the OsO==0 overflow gate on air (the drizzle-wave law).
- `long`'s cross-process nondeterminism (volk alignment) should probably be
  fixed with an over-aligned window buffer, but that touches DSP source that
  has been byte-frozen since 7/09 — flagged, not done.
