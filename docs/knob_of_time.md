# The Knob of Time

*The one knob you can't turn — only consult. This page is the full
math and logic behind the tuner's learned model of **when** each
channel is good, on which antenna, for whatever sky it happens to be
under.*

> **Not to be confused with tune speed.** Fast tuning (the ~10 s
> channel changes) answers *"how quickly can I get this channel on
> screen?"* The Knob of Time answers a different question: *"will
> this channel be watchable at all right now — and if not, when?"*
> No tuning speed rescues a channel whose signal sinks below the
> decode cliff every night; the Knob is what knows the sinking is on
> a schedule. The two cooperate: channels the Knob knows are healthy
> skip ~9 s of cautious re-measuring during a tune.

---

## 1. Why time is a tuning parameter

Hold everything constant — antenna, gain, decoder — and a broadcast
channel's decode margin still walks several dB over a day, with a
stable daily shape. Our multi-day logs (13,800+ measurements)
measured, per channel, swings of **3–7+ dB**:

- one channel only crosses the ~15.2 dB decode cliff in the
  17–21h evening block;
- another holds a 10–21h plateau and collapses after 22h;
- a third is *inverted* — best before dawn, worst in the evening;
- a fourth barely moves at all (the "always-safe" channel).

The physics is classical **diurnal variation**: overnight temperature
inversions duct VHF/UHF (tropospheric ducting — why distant towers
appear at sunrise), foliage moisture shifts path loss, and impulse
noise follows *human* schedules (appliances, chargers, HVAC). None of
it is exotic. What's new here is closing the loop: for a marginal
channel, **the hour is worth more dB than any knob in the receiver**,
so the receiver should learn the schedule and say so.

Lineage, honestly stated: the phenomenon is century-old propagation
science; cognitive radio academia builds *radio environment maps*
(spectrum prediction for transmitters deciding when to use a band).
We found no existing name or implementation for a **consumer receiver
that learns per-channel, per-antenna hour curves from its own decoder
telemetry and advises the viewer** — so this project names it.

## 2. The free sensor

No extra hardware, no probes: the decoder already measures itself.

- **MER** (signal quality, dB) falls out of the equalizer's field-sync
  error at ~41 Hz:

  ```
  MER_dB = 20 · log10(5 / fs_err_rms)
  ```

  (5 = mean |level| of the 8-VSB constellation; full derivation in
  [science.md §12.5](science.md).)

- **Loss** (truth in packets) from the Reed-Solomon block's windowed
  counters: `bad / pkts` per 5 s. Loss is the metric that cannot
  alias: fast faders breathe *between* MER samples and can read
  "flawless" by median while losing 10%/min. When MER and loss
  disagree, loss wins.

## 3. The data model — a CSV is the whole database

One append-only file, `lab/quality_history.csv`:

```
ts, rf, ant, mer, loss_pct, source, date_known
2026-07-10T19:41:02, 36, Antenna B, 19.16, 0.0, panel, 1
```

- **One row per watched minute** (the panel's telemetry recorder),
  **one row per scan dwell** (every channel, every scan), one per lab
  soak. Watching TV *is* the training; there is no training step, no
  database server, no model file. The history **is** the model,
  recomputed on read.
- `ant` is an **opaque string** — whatever label the user's antenna
  picker used. Ownership ("this antenna wins that channel at dawn")
  is a learned statistic over the user's own labels. No antenna name,
  market, or coordinate appears in any code path: the same code
  learns anyone's sky. This is the universality contract.

## 4. The estimator

For channel `c`, hour-of-day bin `h ∈ {0..23}` (local time), over
history rows `i` with ages `Δt_i`:

```
weight      w_i    = 0.5 ^ (Δt_i / H),   H = 14 days (half-life)
estimate    M̂(c,h) = weighted_median( { (mer_i, w_i) : hour(ts_i)=h } )
```

Every design choice below was paid for by a failed experiment:

**Weighted median, not mean.** Impulse bursts throw heavy outliers
into the tail. The median answers "what does a *typical* minute at
19h look like"; the loss column carries the burst story separately.

**Recency decay, because every calibration is a loan.** Gain recals,
antenna moves, and decoder upgrades create *config epochs*; data from
a dead epoch is systematically wrong. H = 14 days lets a stale epoch
fade in about a month — and lets a real seasonal change replace it.

**Confidence tiers, because single-day bins lie.** We watched a
one-afternoon artifact masquerade as a channel's permanent "16h dip"
(191 samples — all from one bad pre-fix afternoon!). Hence:

| tier | requirement | UI treatment |
|---|---|---|
| **solid** | n ≥ 8 samples over ≥ 2 distinct days | trusted, can drive hints |
| **thin** | n ≥ 3 | shown with caveat |
| **borrowed** | neighbor bins h±1 at half weight | labeled as borrowed |
| **unknown** | below all thresholds | rendered "?" — never guessed |

**The hint rule — silence over guessing.** The UI says
"usually better around 21h" only when (a) the *current* hour's bin is
confidently known AND (b) some solid bin beats it by ≥ 2 dB. A model
that mumbles when ignorant is worse than none.

**Antenna ownership** is the same estimator grouped by `ant`: for
each hour, which label's weighted-median MER wins. Territories emerge
from data alone (in our test market, cheap rabbit ears *own* one
channel at 19.9 dB while a better antenna owns another — nobody would
have guessed either).

## 5. Training: two modes that compose

- **Watch-training** (automatic, free): 60 samples/hour flow into the
  bins of whatever is being watched. Deep where the viewer lives,
  blind where they don't. A normal week of evening TV makes the
  evening bins solid.
- **Sweep-training** (`adaptive-tv/time_trainer.py`): a scan measures
  *every* channel once in the current hour bin — but a scan takes the
  tuner (one SDR = one job), so the trainer runs only when the panel
  is idle and stands down the moment anyone tunes in. One sweep per
  hour overnight fills every channel's night bins in ~2 nights.

The arithmetic: solid = 8 samples over 2 days. Hourly idle sweeps
solidify a full night's bins for the entire market in two nights,
while the viewer sleeps.

## 6. Where the model acts

1. **The NERD card** — 24-hour sparkline (`▁▂▅▇`… with `·` for
   unknown hours), this hour's predicted dB, best/worst hours, and a
   live sample counter that ticks up each watched minute.
2. **The guide** — glitchy channels get "usually better around Xh"
   when the data supports it.
3. **The tune path itself** — when the current hour's bin says a
   channel is confidently healthy (≥ 16.8 dB), the tuner skips ~9 s
   of cautious re-measuring and tunes from memory (part of the
   10-second fast-tune stack); when history says cliff — or says
   nothing — the full careful path runs. The Knob never *causes* a
   wrong cliff decision: disagreement with live reality always defers
   to the live measurement.

## 7. Failure modes designed against

- **Cold start:** day one honestly shows "no history yet — watch or
  scan and I'll learn." Nothing is seeded; curves appear within days.
- **Moved antenna / changed rig:** recency decay retires the old
  world in weeks; a deliberate re-scan accelerates it.
- **Renamed antennas:** old and new labels coexist until decay
  retires the old one (cosmetic, self-healing).

## 8. Code map

Everything lives in `adaptive-tv/time_knob.py` (pure stdlib, ~450
lines, imported by the panel; also runnable standalone):

| function | role |
|---|---|
| `record(row)` | append one measurement (called by panel + scanner) |
| `load()` | CSV → rows |
| `curve(rf, rows)` | the 24-bin estimator with confidence tiers |
| `best_hours(rf, rows)` | (best, worst, swing) over confident bins |
| `hint(rf, rows)` | the guarded "usually better around Xh" or None |
| `antenna_by_hour(rf, rows)` | learned territories |
| `nerd_card(rf, rows)` | text sparkline block (also used by the UI) |
| `harvest()/backfill()` | one-time miners for pre-existing lab logs |

Related reading: [science.md §15.5](science.md) (the short version and
the laws that led here), [README](../README.md) for the feature tour.
