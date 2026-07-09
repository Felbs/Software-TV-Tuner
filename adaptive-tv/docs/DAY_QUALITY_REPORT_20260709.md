# Day quality sweep — 2026-07-09 (08:33–18:02)

32 cycles, 408 headless decode-quality measurements across every channel
on every antenna port, plus a UI stress test. Zero lab errors all day.
Quality = real frames + decode errors via ffmpeg null-sink on the
captured stream (the honest metric), MER from the equalizer.

## UI stress test (Phase A) — 2 bugs found, both fixed + verified
- Malformed/empty JSON and missing fields dropped the connection on
  several POST endpoints (tune/meter/record/flat/e7). Now validated →
  clean error message. Verified.
- Connection-burst: 57/244 concurrent requests refused (WinError 10061,
  listen backlog = 5). Raised `request_queue_size` to 128 → 240
  concurrent now 0 errors. Verified.
- State machine + leak audit: clean. Bad channel → graceful "RADIO
  FAILED", recovers, no orphaned tv_live/mpv.
- Added `STVT_PANEL_NOSWEEP=1` (test the API without opening the SDR).

## Channel quality ranking (median over 32 cycles)

| Channel | MER | frames | decode errs | verdict |
|---|---|---|---|---|
| philips RF36 | 19.5 | 959 | 18 | flawless |
| philips RF34 | 19.2 | 474 | 18 | flawless |
| rabbit RF35 | 18.7 | 949 | 18 | flawless |
| philips RF15 | 17.6 | 942 | 47 | clean |
| rabbit RF21 | 17.2 | 473 | 114 | clean |
| rabbit RF34 | 17.0 | 462 | 85 | clean |
| philips RF9 | 15.9 | 387 | 2918 | heavy glitch |
| philips RF7 | 15.2 | 373 | 2541 | heavy glitch |
| discone RF7 | 14.6 | 0 | 2279 | packets, no assemblable video |
| philips RF21 | 12.4 | 0 | — | dead (RF21 is a rabbit channel) |
| rabbit RF7/RF9 | ~9 | 0 | — | dead (VHF weak on rabbit) |
| discone RF9 | 6.2 | 0 | — | dead |

## Findings

1. **The watchability cliff is sharp and confirmed at ~16–17 dB MER.**
   408 measurements draw a clean step: MER ≥17.5 → flawless/clean
   (<120 errors); MER 15–16 → heavy glitch (2500+ errors); MER <13 →
   dead. Nothing lives in between for long — the transition is abrupt.

2. **RF9 improves toward evening (a real time-knob effect).** Decode
   errors held ~3000 from 08:00–15:00, then fell to 2390 at 16:00 and
   **1260 at 17:00** — the channel-9 breathing disease eases in the late
   afternoon. Actionable: channel 9 is most watchable in the evening.

3. **The morning RF15 "anomaly" was a transient, not a channel fault.**
   First readings at 08:00 showed 173 errors; every later hour settled
   to 25–113 (clean). A cold-start artifact — the snap judgment was
   wrong, the day's data corrected it (instrument-audit law: one reading
   lied, 32 told the truth).

4. **Antenna ownership, quantified.** Philips owns UHF (RF36/34/15
   flawless→clean). Rabbit owns RF35 (flawless) + RF21/RF34 (clean).
   Discone owns nothing watchable — RF7 decodes packets but assembles
   0 frames (the polarization ceiling).

5. **The marginal targets are RF9 and RF7 on the Philips** — MER 15–16,
   ~2500 errors, right in the breather band. These are the E7 / marginal
   -recovery frontier (E7 wins below ~5% loss, so these need the low-loss
   moments or diversity/time).

## Next levers (for a Fable 5 session)
- RF9 evening watchability: schedule/prefer evening; test E7 on evening
  RF9 captures (lower loss than midday).
- Per-channel quality now measured → feed the guide's ◉ watchability
  badge and the belief map with these medians.
