# STVT handoff — Raspberry Pi branch (`pi-port-stvt`)

**You're on the Pi branch.** Setup: [docs/install/raspberry-pi.md]
(docs/install/raspberry-pi.md) — clone, `./bootstrap.sh`, `python3
tools/doctor.py`. Everything runs on stock Raspberry Pi OS (64-bit),
standalone: **no Windows, no WSL, no second computer required.** The
optional split-decode mode (Pi serves IQ, a faster Linux box decodes)
is in [docs/pi_split_decode.md](docs/pi_split_decode.md).

Historical engineering notes below (from the WSL-era port this branch
descends from — kept because the SoapyRemote-not-usbip lesson and the
throughput numbers still apply).

---

# STVT handoff — 2026-05-31 (branch `wsl-port-stvt-v2`, the WSL version)

## UPDATE 2026-05-31 (later) — LIVE DECODE WORKS on the Threadripper (WSL)

Direction B (live SDR test in WSL) succeeded. Key finding: **feed the RSPdx via
SoapyRemote-over-TCP, NOT usbip.** usbip attaches the device fine but starves it
to ~1 MS/s (vs 8 requested) — that gappy stream locks the FPLL pilot but kills
segment sync (segs_aligned ~40%, no video). Running PothosSDR's SoapySDRServer
natively on Windows and streaming IQ over TCP sustains 8.04 MS/s, 0 overflows.

Result with full-quality config below: **segs_aligned 96.2%, TEI 0%, OsO ~8/45s,
ffprobe shows mpeg2video 1920x1080 + AC3 + SD subchannels.** Full quality runs
with huge headroom here — the lean Ryzen levers are NOT needed.

Setup (see memory `wsl_sdr_use_soapyremote_not_usbip`):
```
usbipd.exe detach --busid 9-4                                   # release from usbip
"/mnt/c/Program Files/PothosSDR/bin/SoapySDRServer.exe" --bind=0.0.0.0:55132   # IPv4 bind matters
sudo sysctl -w net.core.rmem_max=67108864 net.core.wmem_max=67108864
export STVT_SOAPY_ARGS="driver=remote,remote=127.0.0.1:55132,remote:driver=sdrplay"
export STVT_STREAM_ARGS="remote:prot=tcp"      # key is remote:prot (gr-soapy rejects bare 'prot')
export STVT_RS=stock STVT_VITERBI=hard STVT_EQ=long STVT_SPS=1.5 STVT_TEISCRUB=1 \
       STVT_IFGR=59 STVT_RFGAIN_SEL=5 STVT_ANTENNA="Antenna A"
python3 tools/tv_live.py --rf 34
```
Test harness used: `~/stvt_live_test.sh <label> <secs> [rf]` + `~/ts_analyze.py`.

### Sustained live (for WATCHING, not 45s bursts)
The 1.5/8-tap full-quality config is ~1.28x real-time → OsO creep over minutes.
Use the LEAN real-time config for sustained viewing — verified clean for 11.7 min
(t=700s, OsO=45 total, non-climbing, FPLL locked throughout, 98-100% real, TEI 0%):
`tools/stvt_live_remote.sh [rf]` sets STVT_SPS=1.1 STVT_RRC_SYMS=4 STVT_TEISCRUB=1 + remote args.

### Watching via mpv (WSLg)
`tools/stvt_watch.sh [vid]` — follows live.ts ~25s behind the write head for a buffer
cushion. Critical mpv flags learned the hard way:
- `--vo=wlshm` — WSLg's GPU VO (zink/vdpau/EGL) stalls; software wlshm is smooth.
- `--hwdec=no`, `--cache-pause=no` — never freeze on a brief underrun; plow through.
- Result: 20s cache cushion, A-V 0.000, 0 buffering events.
- mpv `--vid=N` numbering shuffles per start; press `_` in the window to cycle to
  the 1080 HD video track. Audio (AC3) plays via PulseAudio (WSLg).

### Crash-resilience
Launch the server (`Start-Process` on Windows), chain, and mpv with `setsid` so a
Claude-Code session crash doesn't kill them. SoapySDRServer launched via WSL interop
dies with its launching session unless started detached via Start-Process.

---


Software ATSC TV tuner (SDRplay RSPdx → gr-atscplus chain → live.ts → mpv).
This session found that the two long-standing live problems were **software/CPU,
not RF** (proven by deterministic offline replay), and fixed both.

## What was fixed this session

1. **Noise "drift" → erasure RS miscorrection (FIXED).**
   `gr-atscplus/lib/atsc_rs_decoder_erasure_impl.{cc,h}` — added a miscorrection
   guard: an erasure decode whose recovered TS sync byte ≠ `0x47` is a provable
   miscorrection → reject (TEI) instead of emitting garbage. Stops the histogram
   poisoning too. Env `STVT_RS_MISCORR_GUARD=0` restores old behavior (default on).
   NOTE: the current winning config uses `STVT_RS=stock` which sidesteps the
   erasure decoder entirely, so this guard only matters if you re-enable erasure.

2. **Freezing → chain was ~20% too slow for real-time on this CPU (FIXED here).**
   Bottleneck = the matched-filter/resampler at ~98% of ONE core (single-thread).
   Ryzen 1600X (2017) is just slow per-core. Three stacked work-reducing levers
   got it over the real-time line (sustained 100% data past 3min vs prior freeze):
     - `STVT_SPS=1.1`   internal oversampling 1.5→1.1 (1.0 breaks timing recovery)
     - `STVT_RRC_SYMS=4` matched-filter RRC half-span 8→4 (408→158 taps)
     - `STVT_TEISCRUB=0` drop the Python (GIL-bound) TEIScrub block
   `tools/tv_live.py` gained `make_rx_filter()` (tunable-tap RRC, used only when
   `STVT_RRC_SYMS` is set) and `STVT_SPS` support.

## Winning config (env)
```
export STVT_RS=stock
export STVT_VITERBI=hard
export STVT_EQ=long
export STVT_SPS=1.1
export STVT_RRC_SYMS=4
export STVT_TEISCRUB=0
export STVT_IFGR=59
export STVT_RFGAIN_SEL=5
export STVT_ANTENNA="Antenna A"
```
On a FASTER CPU you can raise quality back: `STVT_SPS=1.5 STVT_RRC_SYMS=8
STVT_TEISCRUB=1` (full stock) — should still sustain with headroom.

## Build & run on the new machine
```
cd gr-atscplus/build && cmake .. && make -j$(nproc) && sudo make install   # builds+installs the .so
sudo bash ~/fix_linux_tuning.sh           # performance governor (run once per boot)
# launch (RF34 example): source the env above, then
python3 tools/tv_live.py --rf 34          # chain only, writes tools/data/tv_live/live.ts
# or the supervised chain+player: ~/run_stvt_winner.sh long 34
```
Dependencies that must also be present on the new box (see memory):
- SoapySDRPlay3 ring-buffer patch (262144×32 in SoapySDRPlay.hpp) — rebuild/install.
- gr-atscplus installed as a GNU Radio OOT module.

## Verify it's working (the key metric)
- Watch OsO count: `grep -c OsO tools/data/tv_live/tv_tuner.tv_live.log` — should
  stay LOW and not climb ~1/3s. On a fast CPU it should be near zero.
- Real-data %: tail live.ts, count non-null TS packets. ~100% = video flowing;
  ~100% null = drought (if carrier strong = CPU/OsO, if carrier weak = RF).

## Diagnostic tooling built this session (THE way to separate software vs RF)
- `tools/record_iq.py` — capture raw IQ to a .cf32 file.
- `tools/tv_replay.py` — replay a frozen .cf32 through the EXACT chain offline
  (file source, no SDR). Deterministic ⇒ RF removed from the equation. Supports
  `STVT_DIAG=1` (per-stage taps) and all the STVT_* knobs incl. SPS/RRC_SYMS.
- Home-dir helpers (not in repo): `~/replay_analyze.py` (TS clean/noise timeline),
  `~/eye_analyze.py` (8-VSB eye from eq_out.f32), `~/replay_test.sh`.
- Rule of thumb: if a captured clip decodes clean offline but the live chain
  doesn't, it's real-time/CPU (OsO), NOT RF.
