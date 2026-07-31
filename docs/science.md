# How Software TV Tuner actually works — a long read

This is a guided tour of every signal-processing step that turns a band
of radio noise into watchable television. It assumes you can read code
and have heard the word "frequency," but it does **not** assume an RF
engineering background. Read it start to finish and you should understand:

- Why TV antennas are aimed *horizontally*, and what it costs you if you
  use a vertical one
- What "8-VSB" really means and why it's hard to receive
- What a **Hilbert transform** does, and why the pipeline can't work
  without it
- How a **PLL** locks onto a carrier with no outside help
- What an **equalizer** does, and why it sometimes destroys the signal
  instead of repairing it
- Why **Viterbi decoding** is just dynamic programming over time
- Why **Reed-Solomon** corrects a fundamentally different kind of error
  than the Viterbi stage does
- And why the analog **gain knob** quietly matters more than any of it

…and in **Part Two**, the questions only live experiments could
answer: why tune TV in software at all (the honest silicon-vs-code
scorecard), the seven laws the adaptive era was paid to learn —
metrics that lie, optimizers that cheat, configs that die at sunset
— and how one codebase learned to tune any antenna and tell the
truth about it.

It's structured roughly in the order the signal travels: starting at
the transmitter, ending at your TV's screen.

## 1. The signal at the transmitter

Every full-power TV station in North America transmits an **ATSC 1.0**
digital signal. ATSC was standardized in the early 1990s as the successor
to analog NTSC. It wasn't chosen because it was the smartest option — it
won on political backing and on being cheap to decode in a 1995-era TV
set. We're still living with that choice.

ATSC's modulation is **8-VSB**:

- **"8"** — each symbol carries one of 8 amplitude levels: **±1, ±3, ±5,
  ±7**. That's 3 bits of information per symbol.
- **"VSB"** — *Vestigial Sideband*. An ordinary AM signal carries the
  same information twice, in two mirror-image sidebands. ATSC strips away
  most of the lower one to halve the bandwidth, leaving only a small
  "vestige" behind.

Each channel is **6 MHz wide** — the same slot analog TV used. Packed
inside that 6 MHz:

- A **suppressed carrier** at channel center — no energy is actually
  radiated there; it's just a reference frequency.
- A **pilot tone** 2.69 MHz below center, carrying about **7% of total
  power**. It is the *only* dedicated synchronization energy in the whole
  signal, and it's what the receiver locks onto first.
- The **data sideband**, spanning roughly 0 to +5.38 MHz above the pilot,
  carrying the 10.76 Mbaud symbol stream — almost the entire channel.

The symbol rate is **10.762238 Msym/sec**, chosen as exactly
**(684 / 286) × 4.5 MHz** so it stays commensurate with NTSC's 4.5 MHz
audio subcarrier — a courtesy to coexistence during the analog→digital
transition that finally ended in 2009.

```
Channel 34 (a typical UHF channel) spectrum:

       590 MHz                        596 MHz
         |  pilot    data sideband  |
  noise  |  +        =====================  | noise
─────────┼──┼─────────────────────────────────┼─────────
         590.31  ←—— ~5.38 MHz ——→     595.69
            (pilot 2.69 MHz below center)
```

On a spectrum analyzer it looks like a tilted hat: a sharp pilot spike on
the left, then a flat, noise-floor-like plateau 5.38 MHz wide that is
actually carrying ~19 Mbit/sec of trellis-coded data. The fact that real
data *looks* like noise is not an accident — good digital modulation
spreads energy evenly, and we'll lean on that property more than once.

## 2. Antenna polarization — why horizontals win

Radio waves are electromagnetic waves: an electric field that oscillates
in some direction perpendicular to the wave's travel. When the
transmitting antenna is horizontal, the field oscillates left-to-right —
the wave is **horizontally polarized**.

To capture the most energy, your receiving antenna must oscillate in the
**same plane** as the wave. A vertically-mounted whip catches a
horizontally-polarized wave at maybe 10-20% of the energy a properly
oriented horizontal antenna would — roughly **10-15 dB of SNR thrown
away** before any processing begins.

Here's the trap for SDR hobbyists: in North America **every full-power TV
station transmits horizontally**, but the antennas that ship with SDRs —
discones, vertical whips, the telescoping ones — are built for ham radio,
public safety, and aviation, all of which are *vertically* polarized. So
the default antenna is systematically wrong for TV. This single mismatch
kills more SDR-TV experiments than every algorithm in this document
combined.

- **The cheap fix:** lay rabbit-ears flat in a horizontal "V."
- **The better fix:** a UHF Yagi pointed at the transmitter farm.
- **The pretty fix:** a roof-mounted log-periodic.

## 3. From radio waves to numbers — the SDR

A **Software Defined Radio** like the SDRplay RSPdx is a specialized
analog-to-digital converter that:

1. Tunes a wide RF front end to your channel center (e.g. 593 MHz)
2. Mixes the signal down to baseband (centered at 0 Hz)
3. Filters out everything outside the 6 MHz channel
4. Samples the in-phase (`I`) and quadrature (`Q`) components at 8
   megasamples per second, producing complex IQ samples as pairs of
   int16 values

The output is a flat stream of int16s: `I0 Q0 I1 Q1 I2 Q2 …`. At 8 MS/s ×
4 bytes per complex sample that's **32 MB/sec** — sixty seconds of
capture is ~1.9 GB. Everything downstream is arithmetic on that number
stream.

### Why the gain knob is everything

Between the antenna and the ADC sit analog amplifier stages, and ATSC
8-VSB has a **high crest factor**: its peaks tower several times above its
RMS level. Set the gain too high and those peaks **clip** the ADC's
int16 range, mangling the constellation. Set it too low and the whole
signal sits in the ADC's lowest few bits, where quantization noise
drowns it. The usable window between those failures is narrow, and
landing in it is the single most important thing you do.

On the SDRplay RSPdx, two knobs control it:

- **`rfgain_sel`**: enables/disables stages of the front-end LNA.
  Higher values = fewer LNA stages enabled = less RF gain.
- **`IFGR`** (IF Gain Reduction): post-mixer attenuation in dB
  (inverted: bigger number = quieter).

An early version of this document declared one magic setting "the
empirical sweet spot." That aged badly, and how it aged is one of the
project's best lessons: **there is no universal gain setting.** The
optimum depends on the antenna, the amplifier, the cable, the channel
— and, we eventually proved, the *time of day* (a setting calibrated
at noon became overload garbage at sunset). The real answers live in
Part Two below: measure a live quality signal (MER), grid-search the
two knobs against it per-setup, and hand level-tracking to the
radio's hardware AGC so drift is absorbed in silicon instead of
chased in software.

Get these wrong and they look like perfectly reasonable settings while
producing 100% TEI=1 — i.e. "the decoder is broken" — when nothing
downstream is broken at all. So before suspecting any algorithm, check
the raw IQ histogram:

```python
np.percentile(np.abs(iq), 99.9)  # should be well below 32767
```

If that returns ~30000+, the SDR is clipping. Lower the gain. (This is
the first thing to check in §15's failure table, and it's worth
internalizing now: most "decoder bugs" are gain bugs.)

## 4. Vestigial sideband and the Hilbert transform

VSB is genuinely subtle, so this section takes its time.

### Why VSB exists

A **double-sideband AM** signal centered at carrier `fc`, carrying
information `m(t)`, has a lower sideband (`fc - B` to `fc`) and an upper
sideband (`fc` to `fc + B`). Those two sidebands are redundant: because
`m(t)` is real-valued, its Fourier transform is forced to be
conjugate-symmetric about DC, so each sideband is a mirror of the other.
One of them is enough; the other is pure waste of bandwidth.

**Single-sideband (SSB)** throws one away entirely — but that demands a
brick-wall filter exactly at `fc`, which is hard and expensive to build.
**Vestigial sideband (VSB)** is the engineering compromise: remove *most*
of one sideband but leave a small vestige, so the transition filter can
have a gentle, buildable slope. ATSC keeps a **0.31 MHz vestige** below
the pilot; above it, the data sideband runs out 5.38 MHz. The receiver
has to undo that asymmetry before it can demodulate.

### The Hilbert problem

After the SDR mixes everything to baseband, the waveform is effectively
**real-valued**, and real signals have spectra that are symmetric about
DC: every positive-frequency component implies a mirror-image
negative-frequency one. But ATSC's VSB energy is *not* symmetric — it
lives mostly on one side of the pilot. The honest way to represent a
one-sided spectrum is as an **analytic** (complex-valued) signal, with all
its energy at positive frequencies.

Turning a real signal into its analytic form is exactly the job of the
**Hilbert transform**.

### What the Hilbert transform actually does

Formally, the Hilbert transform `H{·}` of a real signal `x(t)` is
convolution with `1/(πt)`:

```
H{x}(t) = (1/π) · ∫ x(τ) / (t - τ) dτ
```

In the frequency domain it's far cleaner: **multiply every positive
frequency by `+j` and every negative frequency by `-j`** — a 90° phase
shift on every component. The **analytic signal** is then:

```
z(t) = x(t) + j · H{x}(t)
```

Work through the algebra (a good exercise) and you'll find the Fourier
transform of `z(t)` is **zero at every negative frequency** and **doubled
at every positive frequency**. Hilbert-plus-`j` hands you a clean
one-sided spectrum — precisely what a complex PLL needs to see.

### Why ATSC needs it specifically

The pilot sits at +0.309 MHz above the lowest data frequency, which after
baseband mixing lands at **−2.691 MHz** from DC. In a real signal, a tone
at −2.691 MHz has an identical twin at +2.691 MHz. A PLL trying to lock
the pilot cannot tell the two apart, so it splits the difference and locks
to neither — it fails. After the Hilbert transform, the negative-frequency
twin is gone: the PLL sees exactly one tone at −2.691 MHz, locks cleanly,
and the constellation stops spinning.

In code the Hilbert transform is approximated by a **FIR filter** with
antisymmetric taps. The offline/replay path uses scipy's `hilbert()`
(FFT-based, essentially exact); GNU Radio's `hilbert_fc` uses a 65-tap FIR
that's faster but a little less precise near DC.

## 5. Carrier recovery — the FPLL

Even with a one-sided spectrum, the receiver still doesn't know the
*exact* phase or frequency of the transmitter's carrier. SDR front ends
have local oscillators that drift by tens of Hz; transmitters drift too;
propagation adds a little Doppler. Left uncorrected, the constellation
**rotates** at the offset rate — a mere 100 Hz error spins it a full turn
every 10 ms, and you can't slice symbols off a spinning constellation.

The **Frequency-and-Phase-Locked Loop (FPLL)** closes a feedback loop to
kill that rotation:

1. Find the pilot — it's the only constant-amplitude line in the spectrum.
2. Generate an internal **NCO** (numerically controlled oscillator) at the
   expected pilot frequency.
3. Multiply the input by the NCO's conjugate, rotating the pilot toward DC.
4. Measure the **residual phase error** — the I-vs-Q angle of the rotated
   pilot.
5. Nudge the NCO frequency to drive that error to zero, and repeat.

The **loop bandwidth** sets how hard the NCO chases error, and it's a
genuine tension:

- Too wide (high `alpha`): the loop chases every noise spike, adding
  jitter to the output → constellation smear → RS errors.
- Too narrow (low `alpha`): the loop can't keep up with real drift →
  it unlocks → catastrophic failure.

Our `atsc_fpll_tight(alpha, afc_tau_us)` exposes both knobs. Empirically,
**alpha = 0.002, AFC tau = 20 µs** is what survives on real RF at our test
site; stock `atsc_fpll` runs alpha = 0.01, which is too wide for our SNR.
On a healthy lock you'll see the NCO settle near −2.690 MHz with a mean
phase error around 0.035 rad — tight and steady.

> **Implementation note.** This loop runs once per input sample at 8+
> MS/s, so it's one of the live pipeline's CPU bottlenecks. Two opt-in
> optimizations (`STVT_FPLL_FOLD` and `STVT_FPLL_BLOCK_NCO`) shave ~12-14%
> off the live decode cost without changing the output — the math and the
> measurements are in [`cpu-optimization.md`](cpu-optimization.md).

## 6. Symbol timing recovery

Carrier lock fixes *rotation*; it doesn't tell you **when within each
symbol period to sample**. Sample halfway between two symbols and you get
their blurred average; sample at the symbol's peak instant and you get the
best possible signal-to-noise ratio. Finding that instant is timing
recovery.

The classic tool is the **Gardner timing-error detector (TED)**. It takes
three samples per symbol — `prev`, `mid`, `cur` — and computes:

```
err = (cur - prev) · mid
```

The intuition: when timing is right, `mid` lands on a symbol peak. If
you're sampling *early*, `cur` is still climbing toward the next peak, so
`(cur - prev)` is positive while `mid` is large — the product is a
positive error that says "sample later." If you're *late*, the signs flip.
So `err` is a usable gradient pointing at the correct instant. A **PI loop**
(proportional + integral) integrates that gradient into timing
corrections, and the output is one sample per symbol at the optimal point.

**Where it bites:** the Gardner TED's gain scales with the *square* of the
input amplitude. We learned this the painful way — the loop was tuned for
RMS≈1 input, but the SDR was delivering RMS≈0.026, which made the
effective loop bandwidth roughly **600× too small** to track anything. The
fix is one line: normalize to unit RMS before the TED. (Note the pattern —
like the gain knob, this is an *amplitude* bug wearing a *timing* bug's
costume.)

## 7. The equalizer — undoing multipath

A signal doesn't travel straight from tower to antenna. It also bounces
off buildings, hills, and the ground, each echo arriving 1-30
microseconds late and adding to the direct copy. This is **multipath** —
the same thing that gave analog TV its ghostly offset duplicate images.

Formally, the channel convolves the transmitted signal `x(t)` with a
**channel impulse response** `h(t)`:

```
y(t) = ∫ h(τ) · x(t - τ) dτ + noise
```

A clean line-of-sight makes `h(t)` a single spike at delay 0. Heavy
multipath makes it several spikes — at 1 µs, 5 µs, 12 µs — each with its
own complex weight. To recover `x(t)` you must **invert** the channel: apply
`h⁻¹(t)`. The hard part is that you don't know `h(t)` in advance, and it
keeps changing as you move, as weather shifts, as a truck drives past.

### LMS adaptation

The **Least Mean Squares** algorithm learns `h⁻¹` from the signal itself,
in real time:

1. Start with all-zero taps except a single 1.0 in the middle (a delta —
   "assume the channel is already perfect").
2. For each symbol `y[n]`, convolve with the current taps to get output
   `ŷ[n]`.
3. Compare `ŷ[n]` to a known target — but *how* does the receiver know the
   target?
   - **During field syncs:** the PN511 sequence is bit-exact. We know
     precisely what every symbol should be.
   - **Between field syncs:** we **slice** the equalizer output to the
     nearest 8-VSB level (−7…+7) and treat that as the target. This is
     **decision-directed LMS**.
4. Compute the error `e[n] = target − ŷ[n]`.
5. Update the taps: `taps += μ · e[n] · conj(input_history)`.

The step size `μ` is the usual speed-vs-stability dial: small μ learns
slowly but safely; large μ tracks fast but can diverge on a bad decision.

### Why decision-directed LMS sometimes self-destructs

When SNR is low, the slicer's "decisions" are wrong much of the time. LMS
then runs gradient descent toward those *wrong* decisions, pulling the taps
to a solution that maps real data onto incorrect levels. Wrong taps make
the next decision wronger, which corrupts the taps further — a runaway
collapse into garbage.

The defense: only adapt in decision-directed mode when you're already
close to correct (e.g. right after a training pass on a real field sync,
when the constellation already looks 8-level). Otherwise **freeze** the
taps and merely filter with the trained values.

This was a real bug in `atsc_equalizer_long`: it ran DD-LMS on *every* data
segment, steadily undoing its own training. The fix was to `filterN`
(apply trained taps, no further adaptation) on data segments — matching
what upstream gr-dtv does. Which sets up the next section, because the
equalizer can't train at all unless something tells it where the field
syncs are.

## 8. Field-sync spacing validation — the bug that took 21 tiers

The equalizer depends on an upstream **field-sync checker** to announce
"segment N is a field sync — train on its known PN511; the next 312
segments are data, just filter through them." Without that signal,
training never starts.

The checker slides a 511-symbol window across each incoming segment and
counts how many bits disagree with the canonical PN511 sequence. If the
disagreement falls below a threshold (we use 50 of 511, about 10% bit
error), it declares a field-sync hit.

The catch: at a *random* offset in the data stream, your odds of matching
PN511 at 10% error are **not** zero. Repeating content — long runs of
similar bytes, certain video-block edges — can correlate with PN511 well
enough to trip the threshold. Real field syncs arrive exactly **313
segments** apart; these spurious correlations land at random spacings.

Accept one spurious "field sync" and three bad things happen at once:

1. The equalizer trains on a segment that **isn't** PN511 — its taps jerk
   off in a random direction.
2. The segment counter resets to 0 mid-field, mis-numbering all the real
   data segments that follow.
3. Reed-Solomon downstream is fed bytes that look like valid 8-VSB but are
   out of frame. RS only notices ~1.2 seconds later, once the
   deinterleaver buffer fills with misaligned data — which is exactly when
   you *see* TEI=1 explode and the picture freeze.

The fix is a single guard: track segments-since-last-accepted-FS and
reject any candidate whose gap is below tolerance (we use 280; the real
steady-state gap is 313). With that one check, the recurring ~30-second
drift on real RF — the failure that had survived 19 earlier tiers of
equalizer- and Viterbi-side guesswork — vanished completely.

It turns out to be **required for cold-start lock**, too, not just a
cleanup: during convergence the slicer is unreliable, which spawns *extra*
spurious candidates, which prevent the equalizer from ever stabilizing.
Rejecting them lets convergence finish. This isn't a nice-to-have
optimization — on weak channels it's the whole difference between "TEI=100%
forever" and "watching the game."

## 9. Trellis-coded modulation and Viterbi decoding

ATSC protects the data with a **rate-2/3 trellis code**: every 2 input
bits become 3 output bits, which map to one 8-VSB symbol. That extra bit
is structured redundancy the receiver can exploit to fix errors.

Concretely, ATSC uses a **4-state convolutional code** (the lower input
bit runs through a rate-1/2 encoder; the upper bit passes through
unchanged, so 2 bits expand to 3), and runs **12 of these encoders in
parallel**, interleaved across symbols — symbol `n` is handled by encoder
`n mod 12`. The 12-way interleave smears burst errors across multiple
decoders, hardening the link against impulse noise and the NTSC
co-channel interference that was a live concern when ATSC was designed.

### The trellis as a graph

Picture a directed graph with 4 nodes (states). At each time step, a state
can move to 2 of the 4 next states, depending on the next input bit, and
each transition emits one 8-VSB symbol. The encoder simply walks this
graph, one symbol per step. The decoder's job is the inverse: given a
*noisy* version of those symbols, figure out which path the encoder
actually walked.

### Viterbi as dynamic programming

Viterbi solves that efficiently by tracking, for each state at each time
step, the **most likely path so far** ending in that state. To extend one
step:

1. For each candidate destination state, look at the two paths that could
   reach it (from two different previous states).
2. Compute each **branch metric** — how unlikely is it that this noisy
   received symbol came from this transition?
3. Add the branch metric to the source state's accumulated path metric.
4. Keep only the better of the two paths into this state.

After about 16 symbols of look-ahead (the **traceback depth**), the
surviving paths agree on where they were 16 steps ago, so the decoder
commits those bits and emits them. The whole trick is never enumerating
all `2^N` possible paths — at each step you prune to one survivor per
state.

### Hard-decision vs soft-decision

**Hard-decision Viterbi** (gr-dtv's stock) first slices each received
symbol to the nearest 8-VSB level, then runs Viterbi on those committed
guesses. **Soft-decision Viterbi** (our `atsc_viterbi_soft`) skips the
slicer and feeds the *actual* squared distance from the received sample to
each candidate level as the branch metric — so the decoder keeps the
"how close was it" information instead of throwing it away. In theory
that's worth ~3 dB, meaning it can decode a signal 3 dB weaker.

In practice on real RF it buys us about **+0.2 percentage points** of
RS-clean fraction — because the earlier stages (FPLL loop bandwidth,
equalizer convergence) have usually already spent the SNR margin before
Viterbi ever sees it. A useful reminder that a chain is only as strong as
its tightest bottleneck.

## 10. Reed-Solomon — the safety net

Viterbi cleans up most errors, but no code is perfect, so the byte stream
still has occasional damage. ATSC wraps it in an outer **Reed-Solomon
RS(207,187)** code: every 187 data bytes get 20 parity bytes, padding to
207. RS can correct **up to 10 byte errors per 207-byte block**.

RS and Viterbi correct genuinely different failure modes, which is why
ATSC uses both:

- **Viterbi** fixes errors that look like *random per-symbol noise* — the
  steady hiss of low SNR through a working equalizer.
- **Reed-Solomon** fixes errors that look like *bursts of byte corruption*
  — a momentary dropout, an impulsive interferer, a spurious deinterleaver
  swap.

Stacking a burst-corrector outside a noise-corrector is **concatenated
coding**, one of the foundational tricks of modern digital comms.

If RS still can't fix a block (more than 10 bad bytes), it sets the
**Transport Error Indicator (TEI)** bit in that packet's MPEG-TS header. A
player can then drop the packet (a visible glitch) or pass it through and
let the video decoder paper over it.

The **RS-clean fraction** we report throughout the project is just the
percentage of packets with TEI=0. "60% RS-clean" means 60% of packets
sailed through RS untouched and the player has to cope with the other 40%.

## 11. The MPEG-TS multiplex

ATSC's payload is a standard **MPEG-2 Transport Stream**: a continuous run
of 188-byte packets, each beginning with the sync byte `0x47`, each
carrying one of:

- **Video PES** — H.262 (MPEG-2) compressed video. ATSC 1.0 predates
  H.264.
- **Audio PES** — AC-3 (Dolby Digital) audio.
- **PSI tables** — program metadata (the PAT lists programs; each PMT
  lists the components of one program).
- **Null packets** (PID `0x1FFF`) — padding, ignored by everything.

One transmitter usually multiplexes 3-6 sub-channels — a "4.1" HD primary,
a "4.2" SD secondary, a "4.3" 24/7 weather loop, and so on — all sharing
the channel's bit budget. Tune "RF 34" and you receive *all* of them at
once; `--program N` (or the `5.1` / `5.2` syntax in the picker) just picks
which one to display. That's also why `stvt_multirec.py` can record several
subchannels for the price of a single tune: they're already arriving
together.

## 12. Closed captions — line 21, smuggled inside MPEG-2

ATSC captions inherit a wire format designed in 1980 for analog NTSC's
line 21 (CEA-608, originally EIA-608). When the US went digital, the path
of least resistance was to repackage the exact same byte pairs inside the
digital video — so every ATSC stream still carries two CEA-608 channels
(CC1, CC2) alongside the newer CEA-708 captions.

The repackaging works like this:

1. Each MPEG-2 picture (I/P/B frame) may carry a
   `user_data_start_code` section (`0x000001B2`).
2. ATSC marks its own with the identifier `'GA94'` followed by a
   `user_data_type_code`; `0x03` means "this is a `cc_data()` structure."
3. `cc_data()` holds N entries of `(cc_type, cc_data_1, cc_data_2)` — each
   a pair of CEA-608 bytes (or a DTVCC fragment for 708). `cc_type == 0`
   is NTSC field 1, where CC1 and CC2 are multiplexed together.

Three CEA-608 rules from 1980 that the decoder still has to honor:

- **Odd parity** on each 7-bit character — cheap error detection for noisy
  analog VBI.
- **Doubled control codes** — every control pair is sent twice on
  consecutive fields; the receiver discards the duplicate. (Parity catches
  single-bit flips; doubling catches whole-field dropouts. 1980s decoder
  chips couldn't do FEC, so redundancy moved up into the protocol.)
- **Channel multiplexing in field 1** — CC1 and CC2 share one byte stream.
  A control byte's channel is bit 3 of its first byte (`0x10–0x17` = CC1,
  `0x18–0x1F` = CC2), and printable bytes inherit the channel of the most
  recent control. Miss this rule and CC2 Spanish text bleeds into CC1
  English as gibberish.

Digital ATSC adds one more wrinkle. MPEG-2 stores pictures in **decode
order** so B-frames can reference future P-frames — but the broadcaster
wrote the captions in **display order**. A decoder that walks the
elementary stream linearly feeds CC byte pairs to the CEA-608 state
machine out of order, so "THE" arrives as "ETH." Both ccextractor and our
`tools/atsc_cc.py` fix it by reading the 10-bit `temporal_reference` from
each `picture_start_code` and re-sorting captions into display order at
every GOP boundary.

The bundled `tools/atsc_cc.py` implements all of this in pure Python with
no external dependencies: TS demux (PAT → PMT → video PID), GOP-bounded
display-order reorder, CC1/CC2 demux, duplicate-control suppression, and
pop-on / roll-up / paint-on caption buffering.

## 13. Putting it all together: the pipeline

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
Polyphase resample 8 MS/s → 16.143 MS/s = 1.5 samples/symbol
(symbol rate × 1.5 = 10.762238 × 1.5 ≈ 16.143 MHz)
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
ffmpeg + ffplay — demux PSI/PMT, select program, decode MPEG-2 video
and AC-3 audio, render to a window. tv_tuner.py also forks copies
to disk (record) and to RTMP (stream) without re-encoding twice.
```

Every arrow can fail silently, which is why the instrumented
`atsc_fs_checker_inst` block is so valuable: it taps the middle of the
pipeline and prints PN511/PN63 error histograms, and the *shape* of those
histograms usually points straight at which arrow broke.

## 14. Diagnosing failures

A field guide to patterns we've actually hit on this project. (Start at the
top — most "decoder" problems are really front-end problems.)

| Symptom | Probable cause |
|---|---|
| 0 PN511 hits | FPLL never locks. Check input SNR first: gain settings, antenna polarization, raw IQ histogram for clipping |
| Many PN511 hits but `min_pn511_err > 30` | FPLL only barely tracking. Try a tighter loop alpha or a different AFC tau |
| Many PN511 hits, `min_pn511_err = 0`, yet TEI=1 on data | Equalizer divergence. Make sure it isn't running DD-LMS on data segments without first training on field syncs (§7) |
| Plays ~30 s, then collapses to TEI=100% with thousands of distinct PIDs | Field-sync spacing slip — the checker is accepting spurious early candidates. Watch the `[fs_check] rejected_early` count; expect it nonzero on impaired channels (§8) |
| 50%+ RS-clean but the player won't show HD | HD generally needs 80%+. Try a stronger station or a better antenna |
| 100% RS-clean but the picture is scrambled | RS decoded fine, but byte alignment between the deinterleaver and the RS frame is off. Check PN63 polarity |

## 12.5 MER — the dial between "locked" and "decoding"

For most of this project's life, we had exactly two signal-quality
readings: the FPLL's pilot-lock telemetry (`mean|x|` — is the
*carrier* locked?) and the MPEG-2 sequence-header count in the output
TS (did *video* actually decode?). Between those two lies a cliff:
8-VSB's trellis + Reed-Solomon FEC means a channel decodes essentially
perfectly above ~15.2 dB of SNR and produces essentially *nothing*
below it (the industry calls this the "threshold of visibility").
A pilot can lock beautifully at an SNR where the wideband data has no
chance — the pilot is a narrowband tone, robust in exactly the way the
6 MHz of data isn't. So "it locks but won't decode" was a black box:
we couldn't see whether a failing antenna was 1 dB short or 10.

The dial was hiding in the equalizer the whole time. Every ATSC data
field starts with a **field sync** segment whose contents are fixed
and known (PN511/PN63 training sequences at ±5 levels). The LMS
equalizer already computes the RMS error between what it received and
that known training pattern (`fs_err_rms`) — that error *is* the
**Modulation Error Ratio**, the same metric broadcast engineers read
off a $5,000 signal analyzer:

```
MER(dB) ≈ 20 · log10( 5.0 / fs_err_rms )
```

Enable it with `STVT_EQ_TELEM=1` (zero overhead when off) and the
chain prints a continuous, field-sync-rate MER estimate. Its value:

- **It's continuous where decode is binary.** Gain calibration and
  antenna aiming become hill-climbs on a gradient ("13.4 dB, need
  15.2, keep going") instead of guess-and-check against a cliff.
- **It diagnoses by shape.** MER flat as gain rises → the antenna is
  SNR/aperture-limited; no electronics will fix it. MER falls as gain
  rises → front-end overload. MER healthy but zero TS → the problem
  is plumbing (USB throughput, dead stream), not RF. MER oscillating
  ±2 dB on a timescale of seconds → multipath fading.
- **It's the honest aim signal.** In-band power and even spectral
  flatness both peak on multipath reflections that don't decode; the
  equalizer's own training error is the only cheap metric that tracks
  what the FEC actually experiences.

### The universal calibration algorithm

`adaptive-tv/` uses the MER dial to make one code path work on any
antenna. Order matters — each stage rules out a failure class the
later stages would misdiagnose:

1. **Plumbing probe** — stream at 2/6/8 MS/s and count overflows.
   A long/passive USB extension can carry 2 MS/s but starve at the
   6–8 MS/s ATSC needs, which masquerades perfectly as "antenna
   suddenly stopped decoding" (strong carriers, clean pilot, zero
   data, uniformly on every channel).
2. **Interferer census** — coarse spectrum sweep; a big wideband
   antenna can deliver the FM band 10 dB louder than the wanted UHF
   channel, saturating the front end. Cure is hardware notches +
   *less* RF gain, never more.
3. **Port + carrier scan** — in-band shelf (central 6 MHz vs guard
   bands) per antenna port per RF channel. Any active device (mast
   amp, LNA) must be powered before this scan means anything.
4. **MER gain calibration** — sweep the (RF gain × IF gain) grid,
   score each cell by MER, not by lock and not by output-file growth
   (garbage grows at full rate too). The optimum moves every time
   anything in the RF path changes — amp in/out, LNA, even cable
   swaps — so calibrate per-configuration, never hardcode.
5. **Channel survey by MER** — multipath is channel-specific;
   a mux 6 MHz away can be 5 dB better at the same antenna position.
6. **Recovery-config shootout** — for signals riding the cliff edge,
   A/B the erasure-mode Reed-Solomon budget and the equalizer's
   tracking options (gear-shift LMS, quality-triggered tap resets),
   scored by an ffmpeg null-decode judge (fps + concealment-error
   rate), because MER stops discriminating once you're above cliff.
7. **Verdict** — if the best MER is still short of 15.2 dB, report
   *how far* short and why (aperture / overload / multipath /
   plumbing). A 4-dB-short antenna is a hardware problem with a
   number attached, not a software mystery.

### Field results that shaped the algorithm (June–July 2026)

- **An amplified antenna's own amp usually beats an external LNA.**
  The antenna-branded amp is TV-band-filtered; a wideband LNA
  amplifies FM + cellular + everything, and the intermod products
  land in-band. Measured on one directional antenna: its own amp
  MER 15.8 (decodes), wideband LNA alone 6.5, both stacked 13.5
  with the SDR's gain floored — the stack overloads unfixably.
- **Bias-tee LNAs fail silent.** An unpowered LNA is a ~10 dB
  attenuator that still passes enough signal to *look* connected.
  Powering it raised the noise floor +22 dB — instantly visible.
  Know which port your SDR feeds DC on (RSPdx: Antenna B only),
  and that inline filters block DC if placed on the power path.
- **Amplification is not SNR.** A passive indoor panel with a
  genuinely powered 22 dB LNA measured MER 10.1 — the LNA moved
  signal and noise up together. The missing 5 dB is antenna
  aperture; only a bigger/better-sited antenna adds it.
- **The gain knob is two knobs.** On SDRplay hardware, RF gain
  (`rfgain_sel`, front-end LNA stages) and IF gain (`IFGR`,
  inverted: higher = less) trade off against intermod. The same
  MER can hide at several settings; only the grid search finds the
  cell that also has headroom against fades.
- **High MER + persistent errors = impulse noise.** A channel can
  average 19 dB MER (well above cliff) and still glitch, because
  MER is measured at field syncs while microsecond impulse bursts
  corrupt packets between them. The tell: repeated full-scale
  amplitude transients ("rails") in the FPLL telemetry at every
  gain setting. Prime suspect: your own PC — USB 3.0 signalling
  radiates broadband hash centered near 500 MHz, so RF channels
  around there suffer while channels 60+ MHz away are pristine.
  The fix is channel choice and antenna placement, not gain.
- **A huge shelf that never field-syncs is not a TV channel.**
  In-band power says *something* is transmitting; only the ATSC
  field sync proves it's 8-VSB. The strongest "carrier" on one
  scan (+28 dB!) was a phantom — some other service or intermod
  product. Classify by MER-at-any-gain, never by shelf alone.
- **Glitches with a perfect transport stream: the missing-packet class.**
  A stream can show flawless MER and *zero* Reed-Solomon failures and
  still glitch every few seconds — because impulse noise doesn't corrupt
  packets, it snaps the sync chain, and during the fraction-of-a-second
  dropout whole slices of the mux are simply *never emitted*. No error
  counter sees them (there is nothing to count); only the MPEG
  continuity counters betray the gaps, striking every program's PID at
  the same instants. An amplitude-domain impulse blanker helps modestly
  (threshold 2.2–3.0 trimmed the gap rate ~15–30% here), but at ≤2.0 it
  clips 8-VSB's own modulation and the chain emits a full-rate stream
  of pure null padding — which scores *perfect* on any gap or error
  metric, because an empty stream has nothing to be discontinuous. We
  briefly celebrated exactly that mirage. Two lessons: when a "glitchy"
  signal has clean RF numbers, audit continuity, not error flags — and
  every quality metric needs a liveness denominator (confirm real video
  packets exist) before its zero means anything.
- **USB cables are part of the radio — and they fail two different
  ways.** The SDR ships its IQ samples over USB at 32 MB/s, and a
  marginal cable breaks television while every RF number stays
  perfect. Failure one, *starvation*: a long passive extension caps
  the link so hard the radio delivers 2 MS/s when asked for 8 —
  strong carriers, clean pilot, zero data, on every channel at once.
  Failure two is sneakier, *leakage*: a cable that passes a 3-second
  throughput probe at ~96% of the asked rate but drops USB
  micro-bursts every couple of seconds. Each drop is too short to
  unlock the phase loop — MER reads a healthy 19 dB — but each one
  snaps the sync chain and punches a visible glitch. Measured
  back-to-back on the same antenna, channel, and hour: short cable
  0–3 stream gaps/min, long cable 27–33. The probe lesson: measuring
  delivered *volume* is not enough, because a few percent "overhead"
  can actually be the drops — the gap rate during real decode is the
  only honest continuity meter. The practical law: put the shortest
  possible USB 3.0 cable straight into a rear-panel port and never
  touch it again; when the antenna needs to move, extend the *analog*
  side with 75 Ω coax, which carries RF happily across a house. One
  more replug gotcha: the first radio session after any hot-swap
  tends to come up wedged (API init failure, or opens-but-delivers-
  nothing) and the second session works — restart the vendor API
  service after replugging and don't diagnose anything from the
  first attempt.
- **The filter you forgot about is jamming you: audit every default
  against the local band plan.** For months, VHF-hi channels (RF 7–13)
  presented a perfect paradox: the channel scanner locked them and read
  their program guides, but the play chain starved at every gain
  setting, on every antenna. Gain recipes, antenna theories, port
  swaps — nothing moved it. The killer was a single default: the
  SDR's *DAB notch*, a hardware filter meant to reject Europe's
  digital-radio band, enabled since forever because it seemed
  harmless. DAB band III spans 174–240 MHz. American VHF-hi
  television lives at 174–216 MHz. The "harmless" notch was a ~20 dB
  bite out of our own TV channels — the scanner only ever locked VHF
  when its environment happened to omit the notch. The proof took 90
  seconds once telemetry pointed at it: front-end level 16.5 with the
  notch, 170 without, and minutes later channel 7 produced the first
  VHF video in the project's history. Two lessons: a *filter-shaped*
  symptom (level 10× down, uniform, gain-independent, but only on
  certain channels) is a filter, not propagation; and any
  region-specific default in a supposedly universal signal path is a
  bug waiting for a continent to expose it.
- **A good antenna needs no electronics at this location.** The
  same attic antenna measured MER 19.5 bare, 19.5 with its own
  amp, and 19.3 through a powered wideband LNA — but scored
  100/100 quality bare and only 26 through the LNA (extra impulse
  susceptibility + scan blindness from the raised floor). When
  the bare wire already clears the cliff, every amplifier you add
  is pure risk. Amps earn their keep on long coax runs and fringe
  signals, not on the desk.

---

# Part Two — the adaptive era, or: what happens when the tuner
# starts measuring itself

Part One explained how a television signal becomes a picture. Part
Two is about a harder question that took this project weeks of live
experiments to answer: **how do you make one codebase tune ANY
antenna** — a rooftop directional, a $40 flat panel, a ham-radio
discone that was never meant for TV — and tell you the truth when it
can't?

## 14. Why do this in software at all?

A hardware ATSC tuner chip costs a few dollars, draws milliwatts,
locks in a fraction of a second, and has done so reliably since the
Bush administration. Our software chain needs a desktop CPU to do the
same job. So why bother?

**What the chip does better** (the disadvantages of code):

- **Speed and efficiency.** Fixed silicon demodulates in hardware
  pipelines; we burn a CPU core doing math a $3 ASIC does for free.
  A channel change takes it 100 ms; our chain relocks, refilters,
  and relaunches a player in ~30 s.
- **Real-time is unforgiving.** The broadcast never waits. Every
  buffer, disk flush, and scheduler hiccup between the antenna and
  the screen is a chance to drop samples — failure modes a chip
  physically cannot have. Entire days of this project were spent on
  problems (A/V clock runaway, pipe stalls, giant-file chokes) that
  exist only because software is in the loop.
- **Everything is your problem.** Clock sync between the station and
  your sound card, caption renderer state, player recovery — the
  chip's TV set solved these in 2005; you get to solve them again.

**What code does that silicon never will** (the advantages):

- **Visibility.** A chip reports "lock: yes/no." Our chain exposes
  the equalizer's training error (a live broadcast-grade MER meter),
  per-packet continuity forensics, the raw spectrum as a waterfall,
  every intermediate signal. When reception fails, the chip shrugs;
  the code tells you *which physical thing to fix, in decibels*.
- **Adaptability.** The chip's designers froze their assumptions.
  Ours are parameters: Reed-Solomon budgets, equalizer step sizes,
  filter widths, sync policies — all tunable per antenna, per
  channel, per *failure class*, even optimizable overnight by a
  machine (an 834-trial live campaign found configurations no human
  had tried).
- **The floor keeps moving.** A software chain improves after
  shipping. This one gained a signal-quality dial, an impulse-noise
  diagnosis, a self-verifying player, and an adaptive channel-hopper
  in a single week — on the same hardware.
- **You learn the actual physics.** Every struggle above is a lesson
  in how television really works. A chip hides the entire journey.

The honest summary: **hardware is the better television; software is
the better instrument.** This project exists because the instrument
can eventually make ANY antenna into a television — which the chip,
married to its assumptions, cannot.

## 15. The laws the live experiments taught us

Each of these was paid for with a failed evening and is now enforced
by code:

1. **Calibrate per setup, then keep calibrating.** The gain optimum
   moved when we changed antennas, when we added an amplifier — and
   when the sun set. A config that scored perfect at noon was garbage
   at dusk, three times in one day. *No stored configuration survives
   the sky.* Fix: measure MER live, grid-search per setup, and give
   level-tracking to the radio's hardware AGC (microsecond silicon
   servo) so only *regime* choices stay in software.

2. **A metric without a liveness check will eventually lie to you.**
   Our proudest "zero errors!" result turned out to be a stream of
   100 % null packets — nothing to err because nothing was there. An
   optimizer once "won" by making the chain emit silence. Every
   quality number must be conditioned on proof that real content
   exists (video headers per second), or its zero is meaningless.

3. **Offline optimization overfits; live is the only judge.** A
   Bayesian search over frozen signal recordings found a config 56 %
   better than our hand tuning — which scored *zero* on live RF,
   because a frozen recording rewards slowing the equalizer down and
   a breathing sky punishes it. Replay screens candidates; only live
   scoring ships them.

4. **Failures come in classes, and each class has its own cure.**
   Aperture-limited (MER flat vs gain — buy a better antenna),
   overload (MER falls as gain rises — attenuate), plumbing (healthy
   MER, no data — check USB), multipath (MER oscillates — aim), and
   the sneakiest: **missing packets** (perfect error counters, holes
   in the continuity counters — impulse noise snapping sync so
   packets are never emitted at all; no error corrector can fix a
   packet that doesn't exist).

5. **Amplifiers are a loan, not a gift.** Across every antenna
   tested, bare wire matched or beat every amplifier and LNA once
   the software could calibrate honestly — because amplification
   raises signal and noise together, and adds intermod risk on top.
   Amps earn their keep only on long cable runs and fringe signals.

6. **The player is part of the physics.** A live stream cannot be
   paused, cannot wait, and drifts against your sound card's clock
   (measured: +3.4 s/min of A/V runaway on a pipe that couldn't
   seek). The cure was architectural: play from a growing *file*,
   which can seek, so the player can always hop back to the live
   edge — resetting sync and caption state in one motion.

7. **One tuner is a vow of honesty.** A single SDR can watch one
   channel, or sweep a waterfall, or record — never two at once.
   Every feature must negotiate for the radio, and the UI should say
   so plainly instead of pretending (our guide badges every station:
   tuneable, weak, or out-of-reach *on the current antenna*).

## 15.5 The time dimension — teaching the tuner what hour it is

> This section is the short version. The Knob of Time now has its own
> full treatment — estimator math, confidence tiers, training
> arithmetic, code map — in
> [`docs/knob_of_time.md`](knob_of_time.md).

Law 1 said *no stored configuration survives the sky*. This section is
what we built once we accepted that — instead of fighting the sky's
schedule, learn it.

*Terminology, so nobody is confused about what's old and what's new:*
the underlying effect is **diurnal variation** — a century-old term in
radio propagation — driven at TV frequencies by **tropospheric
ducting** and temperature-inversion enhancement, plus man-made
impulse noise that follows human schedules. The idea of a receiver
building a time-aware model of its RF environment also has an
academic cousin (cognitive radio's *radio environment maps*). What we
add is the practical, consumer-grade closing of the loop: a TV tuner
that learns per-channel, per-antenna hour curves **from its own
decoder telemetry**, at zero user effort, and turns them into honest
viewing advice. We searched for an existing community name for that
closed loop and found none — so this project names it: **the Knob of
Time**. (The joke being that it's the one knob you can't turn, only
consult.)

### The phenomenon (measured, not folklore)

Hold everything constant — antenna, gain, decoder — and a channel's
MER still walks several dB over a day. Our multi-day logs (13,800+
samples) show per-channel swings of 3–7+ dB with *stable, repeating
shape*: one channel only crosses the decode cliff in the 17–21h block;
another holds a 10–21h plateau and collapses after 22h; a third is
inverted — best before dawn, worst in the evening; a fourth barely
moves at all. The physics is ordinary: overnight temperature
inversions duct VHF/UHF; foliage moisture changes path loss; and
impulse noise follows *human* schedules (appliances, chargers, HVAC).
The consequence is not ordinary: **for a marginal channel, the hour is
worth more dB than any knob in the flowgraph.** You cannot buy this
SNR — but you can schedule around it.

### The free sensor

If you have the equalizer, you already have the instrument. The
long-equalizer's field-sync error (`fs_err_rms`, one reading per data
field, ~41 Hz) converts directly:

```
MER_dB = 20 · log10(5 / fs_err_rms)
```

(5 is the mean |level| of the 8-VSB constellation; the derivation is
in §12.5.) Packet truth comes from the Reed-Solomon block's windowed
counters (`bad / pkts` per 5 s). One row per watched minute and one
per scan dwell — `(timestamp, rf, antenna_label, mer, loss_pct,
source)` — appended to a plain CSV. The file *is* the model; there is
no training step, no database, no daemon.

### The estimator

For channel `c` at hour-of-day `h` (24 local bins), over history rows
`i` with ages Δt_i:

```
weight     w_i = 0.5 ^ (Δt_i / 14 days)          (recency half-life)
estimate   M̂(c,h) = weighted_median({mer_i, w_i : hour(ts_i) = h})
```

Design choices, each paid for by a failed experiment:

- **Median, not mean.** Impulse bursts put heavy outliers in the
  tail; the median answers "what does a *typical* minute at 19h look
  like," and the loss column carries the burst story separately.
- **Recency decay, because every calibration is a loan** (Law 1).
  Gain recals, antenna moves, and decoder upgrades create *config
  epochs*; a 14-day half-life lets a stale epoch fade in about a
  month instead of haunting the curve forever.
- **Confidence tiers, because single-day bins lie.** A bin is
  **solid** only with n ≥ 8 samples spread over ≥ 2 distinct days —
  one bad Tuesday must not define 3 AM. Below that: **thin** (n ≥ 3,
  shown with a caveat), **borrowed** (neighbor bins h±1 at half
  weight, labeled), else **unknown** — rendered as "?", never
  guessed. The UI only says "usually better around 21h" when the
  current bin is confidently known *and* a solid bin beats it by
  ≥ 2 dB. Silence over guessing.
- **The antenna is an opaque string.** Ownership ("rabbit ears win
  this channel at dawn") is a *learned statistic over labels the
  user's own rig has used* — no antenna name, market, or coordinate
  appears in a code path, so the same code learns anyone's sky.

### Two ways to train it (they compose)

- **Watch-training** — free and automatic: 60 samples/hour flow into
  the bins of whatever you watch. Deep where you live, blind where
  you don't. A normal week of evening TV makes your channels' evening
  bins solid.
- **Sweep-training** — a scan measures *every* channel once, in the
  current hour bin, but it *takes the tuner* (Law 7). So it belongs
  to hours when nobody is watching: an overnight trainer that sweeps
  once per hour fills eight channels × eight hour-bins in a single
  night, and a weekend of idle hours completes curves that
  watch-training alone would take months to reach.

The counting is worth internalizing: solid = 8 samples over 2 days,
so hourly sweeps solidify a full night's bins for the *entire* market
in two nights — while the viewer does nothing but sleep.

## 16. Things the reader can now explain at parties

- All of a market's stations usually ride a handful of transmitters:
  we receive **42 stations from 6 towers**, because unrelated
  broadcasters share multiplexes (Telemundo lives inside NBC's
  6 MHz). Subchannels don't own frequency slices — they share the
  whole channel *by taking turns, packet by packet, thousands of
  times a second*.
- Closed captions ride inside the video stream with per-byte parity
  and decode to English — which makes them a free, always-on
  ground-truth probe: if the captions read clean, the whole chain is
  clean. (This tuner literally reads its own television to grade
  itself.)
- VHF and UHF are different worlds: wavelength changes what an
  antenna can catch, evening tropospheric inversions can add the
  decibels a marginal channel is missing (prime windows: calm summer
  nights, 4–8 AM), and a scanner may lock a channel that the
  playback chain can't yet hold — an open mystery in this repo as of
  this writing.

## 13. Further reading

- **ATSC A/53** — the actual standard:
  https://www.atsc.org/atsc-documents/a53-atsc-digital-television-standard/
- **GNU Radio's `gr-dtv`** — the best free reference implementation:
  https://github.com/gnuradio/gnuradio/tree/main/gr-dtv
- **Wikipedia: 8VSB** — high-level intro:
  https://en.wikipedia.org/wiki/8VSB
- **Wikipedia: Hilbert transform** — the math intro:
  https://en.wikipedia.org/wiki/Hilbert_transform
- **Wikipedia: Viterbi algorithm** — clean walkthrough:
  https://en.wikipedia.org/wiki/Viterbi_algorithm
- **Wikipedia: Reed-Solomon error correction**:
  https://en.wikipedia.org/wiki/Reed%E2%80%93Solomon_error_correction
- **John Proakis — *Digital Communications*** — the textbook behind almost
  everything here. Heavy, comprehensive.
- **Bernard Sklar — *Digital Communications: Fundamentals and
  Applications*** — gentler than Proakis, same material.

---

If you spot a mistake in this explainer, please open a GitHub issue —
for educational material, accuracy matters more than polish.
