---
name: day-quality-baseline
description: "2026-07-09 daytime video-quality sweep (408 measurements, 32 cycles) — per-channel quality baseline + the RF9-evening time-knob finding + ui_lab stress harness"
metadata: 
  node_type: memory
  type: project
  originSessionId: 368b030c-6ab2-4abc-a2a8-33ccdf8dc873
---

First full daytime video-quality sweep (ui_lab.py, 08:33–18:02, 32
cycles, 408 headless null-sink measurements). Report:
adaptive-tv/docs/DAY_QUALITY_REPORT_20260709.md.

**Quality baseline (median decode errors over 32 cycles), watchable → dead:**
- FLAWLESS: philips RF36 (MER 19.5), philips RF34 (19.2), rabbit RF35 (18.7) — ~18 errs
- CLEAN: philips RF15 (17.6, 47), rabbit RF21 (17.2, 114), rabbit RF34 (17.0, 85)
- HEAVY GLITCH: philips RF9 (15.9, ~2900 errs), philips RF7 (15.2, ~2500)
- DEAD: discone RF7 (decodes packets, 0 assemblable frames), philips RF21
  (12.4 — it's a rabbit ch), rabbit RF7/9 (~9), discone RF9 (6.2)

**KEY FINDINGS:**
1. **Watchability cliff is SHARP at ~16–17 dB MER** — 408 pts: ≥17.5 → <120
   errs (watchable); 15–16 → 2500+ errs (heavy glitch); <13 → dead.
   Nothing lives between for long.
2. **RF9 improves toward EVENING (time-knob, quantified)**: errors ~3000
   from 08–15h, fell to 2390 @16h, **1260 @17h**. Channel 9's breathing
   disease eases late afternoon → prefer evening for ch 9, and evening
   RF9 captures are the better E7 test material (lower loss).
3. **Morning RF15 "glitch" was a transient cold-start artifact** (173 errs
   @08h → 25–113 rest of day). Instrument-audit law again: 1 reading
   lied, 32 told the truth — don't trust first-cycle readings.
4. Antenna ownership quantified: philips=UHF (RF36/34/15), rabbit=RF35/21/34,
   discone=no watchable TV (polarization ceiling).

**ui_lab.py = reusable stress+quality harness** (Phase A hammers the panel
API for bugs + leaks; Phase B sweeps all channels for quality). Found+fixed
2 panel bugs: malformed/missing-field POSTs dropped the connection (now
validated), and connection-burst backlog 5→128 (WinError 10061). Added
STVT_PANEL_NOSWEEP=1 to test the API without opening the SDR.

Related: [[fable-finale-builds]] (E7 gradient), [[overnight-cube-campaign]],
[[pending-fable5-work]]
