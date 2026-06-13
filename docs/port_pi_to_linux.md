# Porting the Pi-5 session improvements to `linux-port-stvt-v3` (x86 Ubuntu)

Written 2026-06-13 from the `pi-port-stvt` branch after a multi-day Pi 5 push.
Hand this file to the Claude Code instance on the Ubuntu/Ryzen box (or follow it
by hand). It is self-contained — it does **not** assume the Pi session's context.

## The golden rule

`pi-port-stvt` interleaves **platform-neutral features** (port these) with
**Pi-specific hardware tunes** (do NOT copy — x86 wants its own). This is a
*selective* port, not a branch merge. Cherry-pick the neutral commits; for the
mixed ones, port the logic but keep x86 defaults.

**The Pi's whole premise is "barely enough CPU."** Every Pi default that trades
quality for speed (half-res deinterlace, buffer enlargement, niced player) is
the *wrong* default on a fast x86 box that already runs the chain at several×
real-time. x86 should default to higher quality.

## Phase 0 — setup (on the Ubuntu box)

```bash
cd ~/Software-TV-Tuner            # or wherever the repo lives
git fetch origin
git checkout linux-port-stvt-v3
git pull
git fetch origin pi-port-stvt     # for cherry-picking + diff reference
# Reference the Pi commits with:  git show <sha>   /   git log origin/pi-port-stvt
```

Confirm `git status` is clean before starting. Build/test after **each phase**,
not at the end — the C++ phase especially must be validated in isolation.

---

## Phase 1 — clean platform-neutral ports (cherry-pick; expect some manual fixups)

These are Python/bash robustness + feature commits. `git cherry-pick <sha>`; if
it conflicts (the surfer/scanner files may have diverged between branches), port
the diff by hand using `git show <sha>` as the spec.

| Commit | What it adds | Notes for x86 |
|---|---|---|
| `bbaa46a` | Scanner: `tei_pct` decode-health metric, 20s early-exit dwell, no-retry on "no-growth" channels | Pure win. The dwell early-exits on PAT so the longer ceiling is free. |
| `61cab92` | Supervisors: anchor process-up checks to `^python3 .../tv_live.py` (kills `pgrep -f` false positives) | Pure win — the same false-positive trap exists everywhere. |
| `721baca` | Supervisors: `flock -w 15` (tolerate a dying instance's slow lock release) | Pure win. |
| `397cc6e` | `STVT_MPV_MUTE` env for silent unattended runs | Pure win. |
| `51df2ba` | `stvt_play_hd`: proactive player relaunch on `live.ts` rotation | Pure win. `STVT_ROTATE_RELAUNCH=0` disables. |
| `1c45ac0` | `STVT_MPV_SYNC` knob + 16GB rotation default | Rotation-GB is a disk choice; keep or raise. Sync knob neutral. |
| `b1c6636` | Surfer: caption-feed `ffmpeg -y` fix (feed died after first tune) | Pure bug fix. |
| `7bf55da` | Surfer: real-TV coalesced control loop + `stvt_surf_bot.sh` | Pure win — fixes queued-press replay. |
| `1df46f2` | Surfer: single-instance flock + freeze watchdog (IPC time-pos) + hardened sweep | Pure win. |
| `2e73976` | Surfer: drought-aware freeze recovery (restart chain when edge shows ~1 PID) | Pure win. |
| `da11776` | Surfer: SD-aware display, **5-bar signal meter**, now-playing banner (`stvt_surf_info.py`), dead-channel auto-skip, lock-fd-leak closes, `stvt_surf_stress.sh` | Port ALL of it EXCEPT let the deint default differ — see Phase 3. The lock-fd closes (`exec 8>&-`) are pure wins. |
| `f1c7304` `2afb8c8` `da51b00` `90e00f6` | DVR: `scan` ranking, `verify` subcommand, RAM record, cs16 capture | Platform-neutral DVR features. |

After Phase 1: rebuild nothing (Python/bash only), launch the surfer, confirm
the banner + signal meter render and auto-skip works. Run `stvt_surf_stress.sh`.

---

## Phase 2 — the FPLL fold (C++, the biggest win; needs rebuild + bit-exact A/B)

Commit `ae1ed19` folds `dc_blocker_ff` + `agc_ff` into `atsc_fpll_tight`'s output
loop (`STVT_FPLL_FOLD=1`), freeing ~⅓ of a core. **The C++ is generic (no NEON)
— it helps x86 too.** It also touches `tools/tv_replay.py` and `tools/tv_live.py`
(they skip the separate blocks when the env is set).

```bash
git cherry-pick ae1ed19        # or port gr-atscplus/lib/atsc_fpll_tight_impl.{h,cc} + the python chain edits by hand
cd gr-atscplus/build && cmake .. && make -j"$(nproc)" && sudo make install && sudo ldconfig
```

**Validate bit-exact before trusting it** (same gate used on the Pi): decode one
IQ file twice — `STVT_FPLL_FOLD=0` vs `=1` — and `cmp` the two `.ts` outputs.
They MUST be byte-identical (the fold is a math replica). Then measure the
steady-state speedup. Use the ring-buffer version (the committed one) — an
earlier deque version was *slower*; the commit message documents this.

On x86 decide whether to default it on in the linux launchers (it's a free win,
but verify the A/B on your hardware first).

---

## Phase 3 — adapt, do NOT copy (x86 gets better defaults than the Pi)

These features are worth having, but the Pi's *default* is wrong for x86.

1. **Deinterlace (`STVT_DEINT`).** Port the ladder + the SD-detection logic from
   `da11776`/`95eb138`, but **default x86 to full-res `field` or `frame` deint,
   NOT `lowdeint`.** The Pi uses half-res `lowres=1` decode because it can't
   full-res deint live; x86 has the headroom for proper deinterlacing — better
   picture. Keep the SD-aware window-enlarge (`--autofit-smaller`) — that's
   resolution logic, not a CPU tune. Net: x86 default `STVT_DEINT=field`.

2. **GR buffer enlargement (`STVT_MIN_BUF_BYTES`, commits `05f26b7`/`a4d1f0d`).**
   The Pi needed 8MB/edge to break a lockstep stall at 0.91× real-time. x86
   runs the chain at several× real-time with no lockstep, so this is likely
   unnecessary. Port the *knob* (harmless), but **default it off / unset** on
   x86 unless you measure a benefit.

3. **Gain (`STVT_IFGR`).** `pi-port` defaults `IFGR=50` for the Pi's antenna.
   **Keep `linux-port`'s existing gain** — do not import the Pi value.

4. **`stvt_run.sh` Pi flavor (`7b6928b`).** This bundles several Pi tunes
   (fused, S16, 8MB buffers, niced player, IFGR=50). Don't cherry-pick it
   wholesale. Take only: the `mkdir -p data/tv_live` fix and (optionally) the
   niced-player idea. Leave gain/buffers/eq at x86 values.

5. **Scanner `CHAIN_DEFAULTS` (`99ccc41`).** It modernized the scan chain
   (hard viterbi, fused, early-exit dwell — good) but also set Pi gain/buffers.
   Port the hard-viterbi + early-exit-dwell changes; keep x86 gain.

6. **`stvt_spanish_hunt.py` (`3d98af1`).** Port the **kill-pattern safety fix**
   (it was `pkill -f ffmpeg`-ing its own parent) and the anchored patterns.
   Skip the Pi `DEFAULT_ENV` block (Pi gain/config).

---

## Phase 4 — skip (Pi-only, do not port)

- `b0eadbc` / `24ee248` — **int16 NEON equalizer.** ARM NEON SIMD; won't engage
  on x86. Leave `STVT_EQ_S16` off. (If you want an x86 int16 path, that's a
  separate SSE/AVX project.)
- `eded465` — **ALSA-direct HDMI audio** (`vc4hdmi`/IEC958). Pi hardware quirk;
  x86 uses pulse/pipewire. Keep linux audio.
- `1ada1d2` — windowed 85% autofit was for the Pi's 1360×768 panel. Keep
  whatever linux already does (or adopt if you like windowed).
- `350035e` `11af281` `bc95e76` — **README_PI** / Pi-only docs.
- `bda4d34` `90e00f6`(Pi-DVR framing) `c6892fb` `38a82c5` `9bb45e6` `b331a81`
  `b3a8c26` `d7bbea3` — Pi-DVR / Pi-split-decode / Pi-autobot / Pi diagnostics.
  (The *generic* DVR subcommands in Phase 1 are the portable slice; the
  all-on-Pi framing and Pi feasibility docs are not.)

---

## Validation summary (per phase, on x86)

- **Phase 1:** surfer launches, rich banner + signal meter render, auto-skip
  glides past a dead channel, `stvt_surf_stress.sh` → PASS. Scan produces
  `tei_pct` per locked channel.
- **Phase 2:** `cmp` of fold-off vs fold-on `.ts` is byte-identical; steady-state
  decode speed measured (expect a modest gain; x86 has headroom so it matters
  less than on the Pi).
- **Phase 3:** live TV plays a 1080i channel with full-res deint clean (no
  combing, no excessive drops — x86 should handle it easily); gain/buffers
  unchanged from linux baseline.

## One-line summary for the Ubuntu Claude instance

> Selectively port `pi-port-stvt`→`linux-port-stvt-v3`: cherry-pick the
> platform-neutral surfer/scanner/supervisor/DVR commits (Phase 1) and the C++
> FPLL fold (Phase 2, rebuild + bit-exact `cmp` gate), but give x86 its own
> defaults — full-res deinterlace, no buffer enlargement, linux gain, no NEON
> (Phase 3). Skip Pi-hardware commits (Phase 4). Build and test after each phase.
