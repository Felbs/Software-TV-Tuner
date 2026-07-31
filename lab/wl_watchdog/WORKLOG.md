# WL FRONT-END LOCK FAILURE — root cause, fix, and the acquisition watchdog

Branch `stvt-2.0-wl-speed` (the unified WL + speed build). Offline / build only:
a balloon hunt (`wxTuna/tools/sonde_rx.py hunt --mhz 405.5`) owned the SDR for
the whole session, so **no radio was touched and `radio_lock` was never taken.**

The blocker this closes is `lab/speed_build/WORKLOG.md` §11.5(a): in a minority
of otherwise identical `tv_replay STVT_EQ=wl` runs the fused `atsc_wl_frontend`
never locks at all — `relocks=0`, `segs_aligned=0 (0.00%)`, `fs accepted=0`,
`segs_emitted=291044` instead of `194030`, TS = **0 bytes**. Measured then at
2/39 (~5 %). It was filed as "work-call-boundary state in `general_work`,
needs a watchdog before WL goes anywhere user-facing".

It is not work-call state. **It is a memory bug, and it is now fixed.**

---

## 1. The harness (deliverable 1)

`lab/wl_watchdog/lock_loop.py` — N identical `tv_replay STVT_EQ=wl` runs on one
deterministic 480 MB fixture, parsing the front end's lock telemetry per run
(`segs_emitted / segs_held / segs_aligned / relocks / fs accepted`), the TS size
and md5, and classifying `lock_fail`. Failing runs' logs are always kept;
passing ones are discarded. Everything lands in `loop.jsonl`.

`--debug` sets `ATSC_SYNC_SOFT_DEBUG=1`, which makes the front end print its
correlator state every 256 segments (`peak / rms / snr_ratio / locked /
best_idx`). **That is the channel that solved this** — no rebuild needed to get
the first real evidence.

### It would not reproduce sequentially on an idle box

| batch | runs | lock failures |
|---|---:|---:|
| sequential, idle box | **40** | **0** |
| 8 x lock_loop in parallel (real CPU contention) | **64** | **1 (1.6 %)** |

The original 2/39 was measured while other work was running. So the trigger is
not the signal and not the run count — it is what else the machine is doing.
That single stress failure is the whole diagnosis.

## 2. The failing run, and what it said

`runs/stress1_fail_007.log`, every debug line from the first (seg 256) to the
last (seg 290816):

```
[wl_front] seg=256 peak=-nan rms=-nan snr_ratio=0.00 locked=0 best_idx=0
```

`peak` and `rms` are computed from `d_integrator`, i.e. from the interpolated
symbols. So the symbols were **NaN from the very first segment** — this is not a
lock loop that drifted, it is a block that never had a finite sample.

And the FPLL in the same log is perfectly healthy for the whole 15 s:

```
[fpll t=  0.41s] nco_freq_hz=-2690076.7 mean|x|=0.03781 max|x|=0.3812 in_rms=658.8 out_rms=4.8
```

`out_rms=4.8` is accumulated from every `out[k]`; one NaN would have poisoned it
for the rest of the run. **The input to the front end was finite.** The NaN was
manufactured inside the block — and the only thing the block computes before it
touches a sample is its interpolator taps table.

### Why the whole run then free-runs at exactly 1.5x

`d_mu` is fed by `d_timing_adjust`, which comes from the (NaN) symbol memory, so
`d_mu` goes NaN on the first segment and never comes back (`NaN - NaN = NaN`).
Then:

```cpp
double float_incr = std::floor(s);   // NaN
d_incr = (int)float_incr;            // INT_MIN on x86-64
if (d_incr < 1) d_incr = 1;          // ...clamped to 1, forever
```

so the block advanced **one input SAMPLE per output symbol** instead of 1.5, and
emitted `242 150 000 / 832 = 291 044` segments — the observed number exactly, and
the reason it is exactly the SPS ratio. Nothing else in this loop is sticky: at
normal amplitudes a clamped `d_incr` recovers within a segment. Only NaN is a
one-way door.

## 3. ROOT CAUSE — `0 * NaN` in GR's own FIR kernel

The fused block extracts GR's MMSE interpolator taps by impulse-probing
`gr::filter::mmse_fir_interpolator_ff` — `interpolate(e_j, mu)` returns
`taps[mu][j]`. The probe input was a bare `std::vector<float>(8)`.

GNU Radio 3.10.12's `kernel::fir_filter<float,float,float>::filter()` does not
read `input[0..ntaps)`:

```cpp
const float* ar = (float*)((size_t)input & ~(d_align - 1));   // round DOWN
unsigned al = input - ar;
volk_32f_x2_dot_prod_32f(out, ar, d_aligned_taps[al].data(), d_ntaps + al);
```

It rounds the pointer **down** to volk's alignment (32 B with AVX2) and
dot-products `ntaps + al` floats from there, relying on the leading `al` values
being multiplied by **zero** taps. That is exact arithmetic for finite leading
values. It is not exact for NaN or Inf: `0 * NaN = NaN`, and one NaN term makes
the whole dot product NaN.

GR's own callers always hand `filter()` a pointer into a GR circular buffer,
whose pages are freshly mapped and therefore zero-filled — so upstream never
sees this. A `std::vector<float>(8)` comes off the **recycled C++ heap**, and
MSVC's small-block heap is 16 B aligned, so `al == 4` about half the time and
the kernel reads the 16 bytes immediately before the allocation — whatever the
previous owner of that block left there. If any of those 4 words happens to be a
NaN/Inf bit pattern, **every entry of the taps table is NaN**, because the probe
buffer does not move between the 1032 probe calls.

That accounts for every property of the bug: deterministic input,
nondeterministic all-or-nothing outcome, load-sensitivity (heap contents, not
signal), invisible to `tv_dual` (different allocation history), and absent from
`atsc_sync_soft` (which passes a GR buffer pointer, never a heap array). It has
been there since the fused block was written on 2026-07-27.

### Proved, not inferred

`STVT_WL_PROBE_DIAG=1` (test-only) puts a NaN in the `al` floats before a
deliberately misaligned probe pointer and reports what the kernel returns:

```
[wl_front probe] unpadded_addr=...F70 addr_mod32=16 unpadded_nonfinite=0/1032
                 unpadded_would_have_failed=0
               | poison_al=4 poison_tap=nan poison_makes_nan=1
```

`poison_makes_nan=1` on this build of GR = the align-down read is real and
`0 * NaN` propagates. The same diagnostic re-enacts the pre-fix unpadded probe
each run: over 64 stress runs, `addr_mod32` was 0 in 30 and **16 in 34** — i.e.
half the runs really do take a 4-float back-off, exactly as the mechanism
requires. (`unpadded_would_have_failed` was 0/64 in those runs; the re-enactment
allocates in a slightly different order than the original, so it is a proxy for
the hazard, not a re-run of it. The proof is `poison_makes_nan`, plus the fault
injector below reproducing the field signature byte for byte.)

## 4. THE FIX

`gr-atscplus/lib/atsc_wl_frontend_impl.cc`, new `build_interp_table()`: probe
through the **middle of a zero-filled buffer** with 64 floats (256 B) of padding
on both sides, so the aligned read can only ever touch zeros.

The taps VALUES are unchanged whenever the old code happened to work: the extra
terms were `0 * finite == 0` then and `0 * 0 == 0` now. The post-fix WL decode is
byte-identical to the pre-fix one (§6), as it must be.

Belt and braces, because an all-NaN table is invisible until the decode has
already failed:

* the table is verified finite after every build; a bad one is logged as FATAL,
* the constructor retries up to 4 times (a fresh buffer gets a fresh address),
* the watchdog re-probes the table on every reset, so even a poisoned table is
  recoverable at runtime rather than fatal for the life of the process.

## 5. THE ACQUISITION WATCHDOG (deliverable 2 — defence in depth)

Added regardless of the fix, because "the front end silently produces a 0-byte
TS" is a failure mode that should never be silent again.

Once per **segment** (832 symbols — one integer comparison), and only while
`d_fs_accepted == 0`:

* if no field sync has been accepted within `STVT_WL_WD_SEGS` segments
  (default **1252** = 4 data fields = 2x the measured time-to-first-field-sync,
  which is **seg 135** on this fixture, reproducibly, in all 104 healthy runs),
  reset the timing loop **and** the framing state **and** re-probe the taps
  table, and try again;
* **bounded**: at most `STVT_WL_WD_MAX` resets (default 4), then ONE loud
  `GIVING UP` line and it stops. Never an unbounded loop
  (`mpv_recovery_loop_safety`);
* **explicit success condition**: the first accepted field sync logs
  `RECOVERED ... after N reset(s) — watchdog standing down`, and the watchdog
  never fires again for the life of the block;
* `STVT_WL_WD=0` disables it.

A healthy stream never enters the code (fs accepted at seg 135, window 1252), so
there is nothing to cost and nothing to change.

### Telemetry is additive

`[wl_front FINAL]` keeps its exact historical format. The watchdog gets its own
second line:

```
[wl_front WD FINAL] wd=1 window=1252 max=4 resets=0 gave_up=0 recovered=0 \
                    first_align_seg=1 first_fs_seg=135 segs_seen=194030
```

`first_align_seg` / `first_fs_seg` are new and useful in their own right — they
are the acquisition-latency numbers nobody had before.

### The watchdog is regression-TESTED, not asserted

`STVT_WL_INJECT_NAN=<n>` (test-only) poisons the first n taps tables on purpose
— a deterministic re-creation of the 2 % Heisenbug.

**n=1 — must self-heal:**

```
[wl_front] TEST FAULT INJECTED: taps table #1 poisoned with NaN
[wl_front wd] NO FIELD-SYNC LOCK after 1252 segs (seg=1252 emitted=1252 aligned=0
              relocks=0 fs=0 seg_locked=0) — RESET 1/4
[wl_front wd] RECOVERED: field sync accepted at seg=1491 after 1 reset(s)
[wl_front FINAL] segs_emitted=194447 ... segs_aligned=193167 (99.34%) ... fs accepted=617
```
TS **36 159 168 bytes** instead of 0 — the historical failure now costs 1491
segments (~0.14 s of stream) instead of the entire recording.

**n=99 — must give up, bounded:**

```
RESET 1/4, RESET 2/4, RESET 3/4, RESET 4/4
[wl_front wd] GIVING UP after 4 reset(s) (max=4): this stream cannot be acquired ...
[wl_front FINAL] segs_emitted=291042 segs_held=0 segs_aligned=0 (0.00%) relocks=0 | fs accepted=0
```

Exactly 4 attempts, one give-up line, no loop — **and note that this injected
run reproduces the field signature (291042 / 0.00 % / relocks=0 / fs=0 / 0-byte
TS) to within 2 segments of the 291044 recorded in §11.5(a).** That closes the
diagnosis: the field failure and an injected NaN taps table are the same event.

## 6. THE GATE — failure rate before vs after

Same harness, same fixture, same env, same box.

| build | batch | runs | lock failures | rate |
|---|---|---:|---:|---:|
| pre-fix (`54359c27` DLL) | sequential, idle | 40 | 0 | 0 % |
| pre-fix | 8-way parallel (contended) | 64 | **1** | **1.6 %** |
| pre-fix (7/29, other session) | mixed load | 39 | **2** | **5.1 %** |
| **post-fix (`cac54ce0` DLL)** | 8-way parallel (contended) | **64** | **0** | **0 %** |
| **post-fix** | sequential | **40** | **0** | **0 %** |

Pre-fix pooled: **3 failures in 143 runs (2.1 %)**. Post-fix: **0 in 104**.

Honest statistics: 0/104 against 3/143 is Fisher p ~ 0.09 — suggestive, not
significant on its own. The load-bearing evidence is not the p-value, it is
(1) the mechanism is *proved* (`poison_makes_nan=1`), (2) the injected fault
reproduces the field signature exactly, and (3) the fix removes the only path by
which non-finite memory can reach the taps.

### Nothing else moved

Post-fix WL telemetry, all 104 runs, identical to pre-fix:

```
segs_emitted=194030  segs_aligned=99.99%  relocks=33  fs accepted=620
first_fs_seg=135     TS 36 335 136 bytes  modal md5 AF9769A6
```

## 7. Files

| file | what |
|---|---|
| `lab/wl_watchdog/lock_loop.py` | the N-run lock-failure harness |
| `lab/wl_watchdog/dump_front_in.py` | freeze the front end's input to disk |
| `lab/wl_watchdog/front_only.py` | drive the front end in ISOLATION on those frozen bytes, with work-call chunking knobs (`WLF_MAX_NOUTPUT`, `WLF_IN_BUF`, `WLF_OUT_BUF`) |
| `lab/wl_watchdog/loop.jsonl` | every run ever made by the harness |
| `lab/wl_watchdog/runs/stress1_fail_007.log` | **the failing run** — keep it |

`dump_front_in.py` + `front_only.py` were built to separate "bug inside the
block" from "nondeterministic input" by byte-freezing the input. The debug
telemetry answered the question first, so they were not needed for this fix —
they are kept because they are the right instrument for the next front-end
Heisenbug (they also make chunking a swept variable instead of a waited-for one).

## 8. Env knobs added (all off/inert by default)

| knob | default | what |
|---|---|---|
| `STVT_WL_WD` | 1 | acquisition watchdog on/off |
| `STVT_WL_WD_SEGS` | 1252 | segments per attempt (min 313) |
| `STVT_WL_WD_MAX` | 4 | hard retry cap |
| `STVT_WL_PROBE_DIAG` | off | re-enact the pre-fix probe + the `0*NaN` proof |
| `STVT_WL_INJECT_NAN` | 0 | poison the first n taps tables (watchdog test) |

## 9. What was NOT done

* **No live validation.** The SDR was held all session by the balloon hunt. The
  WL path's live promotion still needs its own OsO==0 gate
  (`drizzle_wave_interferer`) — this session cannot and does not claim one.
* **The `forecast()` C4267 warning at line 480 was left alone** — pre-existing,
  in the untouched verbatim port, and it is a `size_t`->`int` narrowing on a
  value that cannot exceed a few thousand.
* **GR was not patched.** The `0 * NaN` hazard is in
  `gr::filter::kernel::fir_filter::filter()` and every GR block that probes it
  with a heap array is exposed. Fixing it upstream (masking the leading terms
  instead of relying on zero taps) is a real GR bug report, not a change to make
  inside this tree.
