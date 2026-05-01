# Historical findings — early ATSC decode investigation

> **Note:** This document records an early investigation that
> concluded "we're at the antenna SNR ceiling, can't decode."
> That conclusion turned out to be **wrong** — the real issue was
> incorrect SDR gain settings. With the proven recipe in
> [`proven_capture_recipe.md`](proven_capture_recipe.md), software
> decode reaches 60-65% RS-clean and visually matches HDHomeRun.
> The text below is preserved for the methodology and for future
> debugging when *real* SNR-limited captures get studied.

## Question

Can a software ATSC receiver match HDHomeRun on the same RF capture?
HDHR decodes 199 HD frames from an 8-second strong-station capture at
593 MHz UHF. Stock `gr-dtv` decoded 0 HD frames from the same IQ. Why?

(Spoiler: because the early SDR captures used wrong gain settings.
With correct gain, both decode similarly.)

## Methodology

Built a benchmark harness that decodes a locked 8-second `iq.cf32`
baseline and counts:

- HD frames recovered (target: 199)
- RS-clean fraction (TEI=0 packets / total)
- PN511 hit count + histogram (instrumented FS checker)
- PN63 error histogram

Versions tested:

| ver | description |
|-----|-------------|
| v0  | stock `gr-dtv` baseline |
| v1  | 256-tap equalizer, training-only |
| v2  | 256-tap + DD-LMS post-training |
| v3  | 256-tap + DFE (regression) |
| v4  | DFE + soft Viterbi |
| v5  | DD-LMS + soft Viterbi |
| v6  | v5 + instrumented FS checker (no algorithm change) |
| v7  | v6 + tight FPLL (alpha=0.003, AFC tau=20µs) |
| v8  | v6 + relaxed sync (lock=3, force-emit) |
| v9  | v6 + hysteresis-only sync (lock=3, unlock=1) |

## Results (on a low-gain capture — wrong settings)

| ver | total segs | PN511 hits | RS-clean | HD frames |
|-----|-----------:|-----------:|---------:|----------:|
| v0  | 65,951     | —          | 0.0%     | 0/199     |
| v5  | 65,951     | 91         | 0.3%     | 0/199     |
| v9  | 75,475     | 92         | 0.3%     | 0/199     |

RS-clean was pinned at 0.3% across every algorithmic variation. We
believed this meant the antenna was below the SNR ceiling. Later
investigation showed the underlying issue was that the capture itself
was undergained — `rfgain_sel=3` (default LNA chain enabled) was
saturating the ADC. Switching to `rfgain_sel=5` (5 LNA stages off)
recovered the same channel at 62% RS-clean immediately.

## Methodological lesson

When a bunch of algorithmic interventions all produce identical
results, the wall is upstream of the algorithms. We spent rounds
tuning FPLL/sync/equalizer parameters when the actual problem was
that the SDR's analog frontend was clipping. The diagnostic that
would have caught this earlier: histogramming the raw IQ samples and
checking percentile-99 against the int16 max (32767). Saturated
captures show clipping at 99%-ile.

## What's still useful from this session

`atsc_fs_checker_inst` — the instrumented FS checker — is the
single most useful diagnostic this fork added. It prints PN511 and
PN63 error histograms to stderr, which would have shown us
immediately that field syncs were detected at *zero* error count
(meaning carrier locked perfectly) but data segments couldn't
decode (meaning the data SNR was the problem) — vs. an
algorithmic bug which would show degraded sync metrics too.
