# How Magic TV Decoder actually works — a long read

This is a tour of every signal-processing step that turns a stream of
radio noise into watchable television. It assumes you can read code
and have heard of "frequency" but doesn't assume an RF engineering
background. By the end you should understand, in some depth:

- Why TV antennas are aimed *horizontally* and what happens if you
  use a vertical one
- What "8-VSB" actually means and why it's hard to receive
- What a **Hilbert transform** does and why our pipeline can't work
  without it
- How a **PLL** can lock onto a carrier with no help
- What an **equalizer** does and why it sometimes diverges
- Why **Viterbi decoding** is dynamic programming over time
- Why **Reed-Solomon** is a fundamentally different kind of error
  correction than convolutional codes
- Why "the gain knob" matters more than any of the above

It's structured roughly in the order the signal travels: starting at
the transmitter, ending at your TV's screen.

## 1. The signal at the transmitter

Every full-power TV station in North America transmits an **ATSC 1.0**
digital signal. ATSC was designed in the early 1990s as a successor to
analog NTSC. The standard wasn't picked because it was the smartest
choice — it was picked because it had a powerful political champion
and modest computational requirements for 1995-era TV sets. We're
still living with that choice.

ATSC's modulation is called **8-VSB**:

- "8" because each symbol carries one of 8 amplitude values:
  **±1, ±3, ±5, ±7**. That's 3 bits per symbol.
- "VSB" stands for **Vestigial Sideband**. A normal AM radio signal
  has two equal sidebands carrying redundant information; ATSC strips
  most of the lower sideband to save half the bandwidth, leaving just
  a small "vestige".

Each TV channel is **6 MHz wide**, the same slot that analog TV used.
Inside that 6 MHz, ATSC packs:

- A **suppressed carrier** at the channel center (no energy there —
  just a reference frequency)
- A **pilot tone** offset 2.69 MHz below center, contributing about
  7% of the total power. This is the only piece of dedicated
  synchronization energy and is what the receiver locks onto first.
- The **data sideband** spanning roughly 0 Hz to +5.38 MHz above the
  pilot, carrying the 10.76 Mbaud symbol stream. This is most of the
  6 MHz of channel bandwidth.

The symbol rate is **10.762238 Msym/sec** — picked to be exactly
**684 / 286 × 4.5 MHz** so it's commensurate with NTSC's 4.5 MHz audio
subcarrier (a nod to coexistence during the analog→digital transition
that ended in 2009).

```
Channel 34 (a typical UHF channel) spectrum:

       590 MHz                        596 MHz
         |  pilot    data sideband  |
  noise  |  +        =====================  | noise
─────────┼──┼─────────────────────────────────┼─────────
         590.31  ←—— ~5.38 MHz ——→     595.69
            (pilot 2.69 MHz below center)
```

If you saw this on a spectrum analyzer it would look like a tilted
"hat": a sharp pilot spike on the left, then a flat noise-floor-like
region 5.38 MHz wide that's actually carrying ~19 Mbits/sec of
trellis-coded modulated data.

## 2. Antenna polarization — why horizontals win

Radio waves are electromagnetic waves. The electric field component
oscillates in some direction perpendicular to the direction of travel.
When the transmitter's antenna is horizontal, the electric field
oscillates left-to-right; we say the wave is **horizontally
polarized**.

For maximum reception, your receive antenna must oscillate in the
**same plane** as the transmitted wave. A vertically-mounted whip
catches a horizontally-polarized wave at maybe 10-20% of the energy
of a properly-oriented horizontal antenna. That's roughly 10-15 dB of
SNR loss.

In North America, **all full-power TV stations transmit
horizontally**. SDR-hobby antennas like discones and vertical whips
(designed for ham radio, public safety, aviation — all of which use
vertical polarization) are systematically wrong for TV. This single
issue kills more SDR-TV experiments than every algorithmic detail
combined.

The cheap fix: lay rabbit-ears flat in a horizontal "V". The
expensive fix: a UHF Yagi pointed at the transmitter farm. The
pretty fix: a log-periodic antenna mounted on the roof.

## 3. From radio waves to numbers — the SDR

A **Software Defined Radio** (SDR) like the SDRplay RSPdx is a
specialized analog-to-digital converter that:

1. Tunes a wide RF frontend to your channel center (e.g. 593 MHz)
2. Mixes the signal down to baseband (centered at 0 Hz)
3. Filters out everything outside your 6 MHz channel
4. Samples both the in-phase (`I`) and quadrature (`Q`) components at
   8 megasamples per second, producing complex IQ samples encoded as
   pairs of int16 values

The output stream looks like a flat file of int16s: `I0 Q0 I1 Q1 I2
Q2 …`. At 8 MS/s × 4 bytes per complex sample, that's **32 MB/sec**.
Sixty seconds of capture = ~1.9 GB.

### Why the gain knob is everything

Every SDR has analog amplifier stages between the antenna and the
ADC. ATSC 8-VSB has a **high crest factor** — the peak signal
amplitude is several times the RMS amplitude. If your gain is too
high, those peaks clip the ADC's int16 range and the constellation
gets distorted. If your gain is too low, the signal sits in the
ADC's lowest few bits and quantization noise dominates.

For the SDRplay RSPdx, two gain knobs matter:

- **`rfgain_sel`**: enables/disables stages of the front-end LNA.
  Higher values = fewer LNA stages enabled = less RF gain. For ATSC
  on a strong station, you want **fewer LNA stages on**, not more.
  `rfgain_sel=5` (only 2 LNA stages on) is the empirical sweet spot.
- **`IFGR`** (IF Gain Reduction): post-mixer attenuation in dB. Set
  to 59 dB so the AGC-controlled IF stage isn't slamming peaks into
  clipping.

Wrong values look like reasonable settings but produce 100% TEI=1 →
"the decoder is broken". Always check the raw IQ histogram first
before suspecting downstream code:

```python
np.percentile(np.abs(iq), 99.9)  # should be well below 32767
```

If that returns ~30000+, your SDR is clipping. Lower the gain.

## 4. Vestigial Sideband and the Hilbert transform

This is the section the user specifically asked for. Buckle in — VSB
is genuinely subtle.

### Why analog VSB exists

A normal **double-sideband AM** signal centered at carrier `fc` carrying
information `m(t)` has spectrum:

- Lower sideband from `fc - B` to `fc`
- Upper sideband from `fc` to `fc + B`

These two sidebands carry redundant information because `m(t)` is
real-valued, which forces its Fourier transform to be conjugate-
symmetric about DC. So one sideband is enough; the other can be
thrown away to halve the bandwidth.

**Single-sideband (SSB)** does exactly this: filter out one sideband
entirely. But SSB requires a brick-wall filter at exactly `fc`, which
is hard to build cheaply. **Vestigial-sideband (VSB)** is a
compromise: filter out *most* of one sideband but leave a small
"vestige" so the cutoff filter doesn't have to be infinitely sharp.

ATSC keeps a **0.31 MHz vestige** below the pilot. Above the pilot,
the data sideband extends 5.38 MHz. The receiver has to undo this
asymmetry before it can demodulate.

### The Hilbert problem

After the SDR mixes everything down to baseband, the signal looks
like a **real-valued** waveform (because the SDR's analog LO mixed it
that way). But real signals have spectra that are symmetric around
DC: a positive-frequency component implies a mirror-image negative-
frequency component. ATSC's VSB structure is *not* symmetric — it's
mostly above the pilot. So the natural way to express this is as an
**analytic** (complex-valued) signal, with everything in positive
frequencies.

Going from a real signal to its analytic form is exactly the job of
the **Hilbert transform**.

### What the Hilbert transform actually does

Mathematically, the Hilbert transform `H{·}` of a real signal `x(t)`
is the convolution with `1/(πt)`:

```
H{x}(t) = (1/π) · ∫ x(τ) / (t - τ) dτ
```

In the frequency domain it's much cleaner: **multiplying every
positive frequency by `+j` and every negative frequency by `-j`**.
That's a 90° phase shift applied to every component.

The **analytic signal** is then defined as:

```
z(t) = x(t) + j · H{x}(t)
```

You can prove (do it as an exercise) that the Fourier transform of
`z(t)` is **zero for all negative frequencies** and **doubled for
positive frequencies**. So Hilbert + j-add gives you a one-sided
spectrum — exactly what the receiver needs to feed into a complex
PLL.

### Why we need it for ATSC specifically

The ATSC pilot lives at +0.309 MHz above the lowest data frequency,
which after baseband mixing lands at -2.691 MHz from DC. A real
signal at -2.691 MHz has a mirror image at +2.691 MHz. The PLL,
trying to lock onto the pilot, can't tell those apart and locks to
the average — i.e. fails.

After Hilbert, the negative-frequency mirror is gone. The PLL sees
exactly one tone at -2.691 MHz, locks cleanly, and the constellation
stops rotating.

In code, the Hilbert transform is approximated by a **FIR filter**
with antisymmetric taps. We use scipy's `hilbert()` which does it via
FFT for exactness. GNU Radio's `hilbert_fc` block uses a 65-tap FIR
which is faster but less precise near DC.

## 5. Carrier recovery — the FPLL

Even after Hilbert, the receiver doesn't know the *exact* phase or
frequency of the transmitter's carrier. SDR analog frontends have
local oscillators with tens-of-Hz drift; transmitters have their own
drift; ionospheric propagation adds a small Doppler shift. If
uncorrected, the constellation rotates at the offset rate — a 100 Hz
error means the constellation does a full revolution every 10 ms,
making it impossible to slice symbols.

The **Frequency PLL (FPLL)** solves this with a feedback loop:

1. Find the pilot tone in the received spectrum (it's the only
   constant-amplitude line)
2. Generate an internal NCO at the expected pilot frequency
3. Multiply the input by the conjugate of the NCO — this rotates the
   pilot toward DC
4. Measure the residual phase error (the I-vs-Q angle of the rotated
   pilot)
5. Adjust the NCO frequency to drive that error to zero

The **loop bandwidth** controls how aggressively the NCO chases
errors:
- Too wide (high alpha): the loop tracks every random noise spike →
  noisy output → constellation jitter → RS errors
- Too narrow (low alpha): the loop can't keep up with real frequency
  drift → unlocks → catastrophic decode failure

Our `atsc_fpll_tight(alpha, afc_tau_us)` exposes both knobs.
Empirically, **alpha=0.002, AFC tau=20µs** is the value that wins on
real RF for our test setup. The default `atsc_fpll` uses
alpha=0.01 — too wide for our SNR.

## 6. Symbol timing recovery

Even with perfect carrier lock, you need to know **exactly when in
each symbol period to sample**. Sampling halfway between two symbols
gives you the average of two adjacent values — not useful. Sampling
at the symbol's peak instant gives you the maximum signal-to-noise
ratio.

The classical algorithm is the **Gardner timing-error detector
(TED)**. It uses three samples per symbol — call them `prev`, `mid`,
and `cur` — and computes:

```
err = (cur - prev) · mid
```

Why this works: at the optimal sample point, `mid` lands on a
symbol's peak; if your timing is early, `cur` is still climbing
toward the next peak so `(cur - prev)` is positive while `mid` is at
its peak (large) — this gives a positive error signal that nudges
the sample clock later. If timing is late, the signs flip. So the
error is a useful gradient.

A **PI loop** (proportional + integral) takes the Gardner error and
generates timing corrections that converge to zero error. Output: one
sample per symbol, sampled at the optimal instant.

**Why it fails:** the Gardner TED's gain depends quadratically on the
input amplitude. Our pipeline learned this the hard way — the loop
was tuned for RMS=1 input but our SDR delivered RMS=0.026, making
the loop bandwidth 600× too small. Fix: normalize the input to
unit-RMS before the TED.

## 7. The equalizer — undoing multipath

Real RF signals don't go straight from the transmitter tower to your
antenna. They also bounce off buildings, hills, and the ground,
arriving 1-30 microseconds later. Each bounced copy adds to the
direct copy at your antenna. This is **multipath** — and it's why
old analog TV used to have ghosts (faint copies offset to the right
of the main image).

Mathematically, the channel transforms the transmitted signal
`x(t)` into the received signal `y(t)` via convolution with the
**channel impulse response** `h(t)`:

```
y(t) = ∫ h(τ) · x(t - τ) dτ + noise
```

For a clean line-of-sight, `h(t)` is a single spike at delay 0. For
heavy multipath, `h(t)` has multiple spikes at delays of 1µs, 5µs,
12µs, etc., each weighted by a complex amplitude.

To recover `x(t)`, we need to **invert** the channel — apply the
inverse filter `h⁻¹(t)`. The challenge: we don't know `h(t)` in
advance, and it changes constantly (as you move, weather changes,
trucks drive by).

### LMS adaptation

The **Least Mean Squares** algorithm is a way to learn `h⁻¹` from
the received signal in real time:

1. Start with all-zeros taps except a 1.0 in the middle (delta
   function — pretend the channel is identity)
2. For each received symbol `y[n]`, convolve with current taps to
   get an output `ŷ[n]`
3. Compare `ŷ[n]` to a known target — when does the receiver know
   the target?
   - During **field syncs**: the PN511 sequence is bit-exact. We
     know what every symbol *should* be.
   - Between field syncs: we **slice** the equalizer output to the
     nearest 8-VSB level (-7, -5, -3, -1, +1, +3, +5, +7) and
     pretend that's the target. This is **decision-directed LMS**.
4. Compute the error `e[n] = target - ŷ[n]`
5. Update the taps: `taps += μ · e[n] · conj(input_history)`

The step size `μ` controls how aggressively taps update. Small μ
means slow learning but stable; large μ means fast tracking but
divergence on bad decisions.

### Why DD-LMS sometimes diverges

If the input SNR is low, the slicer's "decisions" are wrong half the
time. The LMS update is gradient-descent on those wrong decisions —
the taps drift toward a solution that maps the actual data to
*wrong* levels. Once the taps are wrong, the next decision is even
more wrong, and the equalizer collapses to garbage.

The defensive trick: only do DD-LMS adaptation when you're already
close enough to the right answer (e.g. when training-LMS just
finished and the constellation looks roughly 8-level). Otherwise
freeze the taps and just **filter** with the trained values.

This is the bug we hit in `atsc_equalizer_long`: it was running
DD-LMS on every data segment, undoing the training. The fix was to
just `filterN` (apply trained taps without further adaptation) on
data segments — match what upstream gr-dtv does.

## 8. Trellis-coded modulation and Viterbi decoding

ATSC encodes data with a **rate 2/3 trellis code**: every 2 input
bits become 3 output bits, which then map to one 8-VSB symbol. The
extra bit adds redundancy that lets the receiver correct errors.

Specifically, ATSC uses a **4-state convolutional code** with 12
parallel encoders interleaved across symbols. Symbol `n` is encoded
by trellis encoder `n mod 12`. This spreads burst errors across
multiple decoders, improving robustness against impulsive
interference.

### The trellis as a graph

Picture a directed graph with 4 nodes (states). At each time step,
each state can transition to 2 of the 4 next states (depending on
the next input bit). Each transition produces one 8-VSB output
symbol.

The encoder walks this graph, emitting one symbol per step. The
decoder's job is to figure out which path through the graph the
encoder walked, given a noisy version of the symbols.

### Viterbi as dynamic programming

For each state at each time step, track the **most-likely-so-far
path** that ends in that state. To extend by one step:
1. For each candidate destination state, look at the two paths that
   could lead to it (from two different previous states)
2. Compute the **branch metric**: how unlikely is it that the
   noisy received symbol would come from this transition?
3. Add the branch metric to the previous-state's path metric
4. Keep the better of the two paths into this state

After ~16 symbols of "look-ahead" (the **traceback depth**), the
algorithm is confident about the path 16 steps back, and it emits
the corresponding decoded bits.

### Hard-decision vs soft-decision

**Hard-decision Viterbi** (gr-dtv's stock) takes each received
symbol, slices it to the nearest 8-VSB level, then runs Viterbi on
those binary decisions. **Soft-decision Viterbi** (our
`atsc_viterbi_soft`) skips the slicer and uses the actual squared
distance from the received symbol to each candidate level as the
branch metric. Soft is theoretically ~3 dB better — meaning it can
decode signals that are 3 dB weaker than hard-decision can manage.

In practice on real RF, soft Viterbi gives us about +0.2 percentage
points of RS-clean fraction. The other steps in the chain (FPLL
loop bandwidth, equalizer convergence) eat most of the SNR margin
before Viterbi sees it.

## 9. Reed-Solomon — the safety net

After Viterbi, the byte stream still has occasional errors (because
no error-correcting code is perfect). ATSC adds an outer **Reed-
Solomon RS(207,187)** code: every 187 data bytes are protected by 20
parity bytes, padding to 207 bytes. RS can correct **up to 10 byte
errors per 207-byte block**.

RS is fundamentally different from Viterbi:

- Viterbi corrects errors that look like *random* symbol noise
  (the kind you get from low SNR with a working equalizer)
- RS corrects errors that look like *bursts* of byte corruption
  (the kind you get from a momentary signal dropout, an impulsive
  interferer, or a spurious deinterleaver swap)

The two complement each other. Together they implement
**concatenated coding** — a foundational technique in modern
digital communications.

If RS gives up on a packet (more than 10 byte errors), it sets the
**Transport Error Indicator (TEI)** bit in the MPEG-TS header to 1.
A media player can either drop those packets (causing visible
glitches) or pass them through and let the video decoder do its
best.

The **RS-clean fraction** we measure is the percentage of decoded
TS packets with TEI=0. 60% RS-clean means 60% of packets passed RS
without errors; the other 40% were flagged and the player has to
decide what to do with them.

## 10. The MPEG-TS multiplex

ATSC's payload is a standard **MPEG-2 Transport Stream**: a
continuous stream of 188-byte packets, each starting with a sync
byte (0x47), each carrying one of:

- **Video PES**: H.262 (MPEG-2) compressed video. ATSC-1.0
  predates H.264.
- **Audio PES**: AC-3 (Dolby Digital) compressed audio.
- **PSI tables**: program metadata (PAT lists programs, PMT lists
  components per program, etc.)
- **Null packets** (PID 0x1FFF): padding, ignored by everything

A single transmitter typically multiplexes 3-6 sub-channels — e.g.
a "4.1" HD primary, "4.2" SD secondary, "4.3" 24/7 weather, etc.
All share the same RF channel and bit budget. When you tune to "RF
34", you receive ALL the sub-channels simultaneously; VLC's
"Playback → Program" menu lets you pick which one to view.

## 11. Putting it all together: the pipeline

```
Antenna (horizontal, pointed at transmitter tower)
       ↓ ~1 microvolt RF
RF amplifier (in the antenna or a separate inline LNA)
       ↓ ~10 millivolts RF
SDR analog frontend (mixer, IF amp, anti-alias filter)
       ↓
SDR ADC at 8 MS/s, complex IQ
       ↓ stream of int16 pairs
Hilbert transform (analytic signal: only positive freqs)
       ↓
Frequency shift to put pilot at -2.691 MHz
       ↓
SRRC matched filter (matches transmit pulse shape)
       ↓
Resample to 6.25 MS/s = exactly 1.5 samples/symbol
       ↓
FPLL — locks onto pilot tone, removes carrier rotation
       ↓
DC blocker — removes pilot DC component
       ↓
AGC — normalizes amplitude
       ↓
Symbol-timing recovery (Gardner TED + PI loop)
       ↓ one sample per symbol, ±7 amplitude levels
Field-sync checker — finds segment boundaries via PN511
       ↓ segments tagged with field#, segment#
LMS Equalizer — undoes multipath, trains on PN sequences
       ↓ clean 8-VSB symbols
Viterbi decoder — undoes trellis coding
       ↓ bytes
Deinterleaver — undoes byte interleaving
       ↓
Reed-Solomon decoder — fixes up to 10 byte errors per 207-byte block
       ↓
Derandomizer — undoes the LFSR scrambler the transmitter applied
       ↓
Depad — strips ATSC framing back to MPEG-TS 188-byte packets
       ↓
TEI scrub — rewrites RS-failed packets to NULL (preserves continuity)
       ↓
VLC — demuxes PSI, decodes H.262 video and AC-3 audio, displays
```

Every arrow has a way to silently fail. The instrumented
`atsc_fs_checker_inst` is invaluable because it taps into the
middle of the pipeline and prints PN511/PN63 error histograms —
which step is broken usually shows up as a particular shape in
those histograms.

## 12. Diagnosing failures

A few common patterns we've seen in this project:

| Symptom | Probable cause |
|---|---|
| 0 PN511 hits | FPLL never locks. Check input SNR (gain settings, antenna polarization, raw IQ histogram for clipping) |
| Many PN511 hits with `min_pn511_err > 30` | FPLL barely-tracking. Try tighter loop alpha or different AFC tau |
| Many PN511 hits with `min_pn511_err = 0` but TEI=1 on data | Equalizer divergence. Check that your equalizer isn't running DD-LMS on data segments without first training on field syncs |
| 50%+ RS-clean but VLC won't play HD | Need 80%+ for HD. Try a stronger station or better antenna |
| 100% RS-clean but VLC shows scrambled | RS decoded, but byte alignment between deinterleaver and RS frame is wrong. Check PN63 polarity |

## 13. Further reading

- **ATSC A/53** — the actual standard:
  https://www.atsc.org/atsc-documents/a53-atsc-digital-television-standard/
- **GNU Radio's `gr-dtv`** — best free reference implementation:
  https://github.com/gnuradio/gnuradio/tree/main/gr-dtv
- **Wikipedia: 8VSB** — high-level intro:
  https://en.wikipedia.org/wiki/8VSB
- **Wikipedia: Hilbert transform** — math intro:
  https://en.wikipedia.org/wiki/Hilbert_transform
- **Wikipedia: Viterbi algorithm** — clean explanation:
  https://en.wikipedia.org/wiki/Viterbi_algorithm
- **Wikipedia: Reed-Solomon error correction**:
  https://en.wikipedia.org/wiki/Reed%E2%80%93Solomon_error_correction
- **John Proakis — *Digital Communications***: the textbook on
  everything in this document. Heavy but comprehensive.
- **Bernard Sklar — *Digital Communications: Fundamentals and
  Applications***: more accessible than Proakis, same material.

---

If you spot a mistake in this explainer, please open a GitHub issue
— accuracy matters more than smoothness for educational material.
