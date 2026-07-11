---
name: sdr-firmware-replug
description: "After multiple SDR daemon restarts, SDRplay firmware can get stuck producing pure noise output even though FPLL shows locked. ONLY fix is physical USB replug — daemon-side restarts don't reset the firmware. Detectable via the noise fingerprint."
metadata:
  node_type: memory
  type: project
  originSessionId: ubuntu-mempalace-import-2026-06-13
---

**The failure mode:** chain looks fully alive — FPLL locked, blocks running,
TS bytes flowing — but the output is pure-noise random bytes (no real
MPEG-TS structure). All "video gone" symptoms (player won't decode, ffprobe
shows "Invalid frame dimensions" forever).

**The root cause:** SDRplay firmware ends up in a bad state after multiple
SoapySDR daemon restarts. The daemon restart doesn't reset the SDR's onboard
calibration — only physical USB power-cycle does. Once stuck, no software
lever (gain, AGC, RS variants, FPLL alpha, NB) recovers it.

**Fix recipe (Ubuntu, where this was diagnosed):**
1. **Physical USB replug** of the SDR — pull the cable on the SDR end (not
   just the hub-PC end). The SDR re-enumerates on a new USB path
   (e.g. `1-7.4` → `1-8.4`).
2. Daemon picks up the fresh device automatically.
3. Restart chain with the bare default config (STVT_RS=stock, STVT_VITERBI=hard,
   STVT_EQ=long, no overrides).

**Verify it's working:** MPEG-2 markers in live.ts: `seq_header > 50` per 50MB
sample, structured PID distribution (not uniform). See [[noise-fingerprint-check]]
for the detection commands.

**On Windows:** same failure mode is plausible with SDRplay on USB — the
firmware behavior is hardware-level, not OS-level. If a Windows session shows
chain-alive-but-noise-output, try the physical replug before swearing at the
DSP code. Daemon equivalent on Windows is `sdrplay_apiService.exe`.

**Don't do** when video is gone:
- Don't immediately blame RF — run the noise fingerprint first.
- Don't sweep DSP knobs — they don't fix firmware state.
- Don't trust "FPLL locked" as proof the SDR is sane — it locks on noise too.

**How to apply:** any "video gone after working earlier" report on either
Ubuntu or Windows — check noise fingerprint first, then replug if uniform.
Saves hours of bogus chain-config sweeping.

**2026-07-10 TRIPLE-STACK RECOVERY PROTOCOL (a week of campaign churn
triggered the stick; three layers each masked the next):**
1. KILL THE PANEL FIRST — its free-running spectrum sweeper contends
   with every recovery attempt (sessions open but stream 0 samples).
2. User replugs USB (the only firmware-stick cure).
3. WAIT for Windows enumeration (Get-PnpDevice shows RSPdx OK), THEN
   Restart-Service SDRplayAPIService (bouncing before enumeration
   races and stays deaf).
4. Eat one throwaway session (always 0%), verify session 2 hears RF
   (IQ RMS > 20), then relaunch panel.
Scanner now detects the all-(-inf) deaf sweep and prints "replug the
SDR" honestly (tv_tuner.py, 1c1409a) instead of letting the dud-guard
silently keep a stale map.

Related: [[noise-fingerprint-check]], [[linux-chain-stability-recipe]],
[[scanner-chain-env-gotcha]].
