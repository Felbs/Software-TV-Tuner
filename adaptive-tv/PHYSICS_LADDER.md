# The Physics Ladder — untapped radio science for the next level
*Deep-think, 2026-07-06 late night. Each entry: the physical law, the
evidence already in OUR data, and the concrete build. Ranked by
(expected dB or capability) / effort.*

## P1. Coherence time — match adaptation speed to the channel's speed
**Physics.** A multipath channel changes at a rate set by the motion of
its reflectors (Doppler). The autocorrelation of the CIR over time
gives the coherence time τc. Information theory is blunt: an adaptive
filter should carry exactly as much bandwidth as the channel varies —
adapting faster than τc means tracking noise (we re-learned this as
"replay overfits" and the DD death spiral); slower means lag (RF15's
oscillation outruns the FS-anchored LMS).
**Our evidence.** RF7's "breathing canyons" (27.6 dB swings), RF15's
12↔17 dB oscillation, RF21's evening 41%-loss at healthy MER — all are
channels whose VARIATION RATE, not average quality, is the killer.
**Aircraft flutter suspicion:** we sit near the DCA flight path;
aircraft scatter produces quasi-periodic 0.5–10 Hz fading — the classic
ATSC impairment. Nobody has ever checked our fade periodicity.
**Build.** (a) cir_dump time series → CIR autocorrelation → τc per
channel/antenna, logged to the cube (one number per sample: "channel
speed"). (b) FFT the fs_err_rms time series from any chain log —
a spectral peak at 0.5–10 Hz = aircraft flutter, DIAGNOSED. (c) Gear
the LMS (µ, FS_AVG_DEPTH, DD gate) from measured τc — the adaptive
tuner learns not just the channel but the channel's TEMPO.

## P2. Weather-coupled propagation forecasting (the time-knob's engine)
**Physics.** VHF/UHF over-the-horizon enhancement is refraction:
the radio refractivity N = 77.6 P/T + 3.73e5 e/T² — pressure,
temperature, humidity. A temperature inversion (cold ground under warm
air) bends signals earthward: ducting. Dawn radiative inversions are
why channel 9's window is 05:30–06:30 — that's not luck, it's
meteorology, and meteorology is FORECASTABLE.
**Our evidence.** The measured dawn window; "best channel is a function
of time"; tropo dawn on the discone; the 13-cycle overnight.
**Build.** Pull hourly METAR (DCA) + NWS forecast: surface temp trend,
dew point spread, pressure, wind (calm clear nights = strong
inversions). Score each coming dawn 0–10 for inversion strength; the
ambush self-schedules (skip windy/rainy dawns, double down on ducting
mornings). Later: Hepburn tropo index scraping. The map stops being a
lookup table and becomes a FORECAST.

## P3. Detection precedes decoding — the pilot rising-edge tripwire
**Physics.** Detecting a carrier takes ~20 dB less SNR than decoding
its payload (narrowband coherent integration vs 10.76 Msym/s data).
The ATSC pilot is a CW tone we can watch minute-by-minute for the
leading edge of an enhancement — long before video is possible.
**Our evidence.** mean|x| (pilot proxy) already streams in telemetry;
RF9's bell curve had a ~90 min rising edge we only saw in hindsight.
**Build.** Sentinel mode: park on the target between cube duties,
integrate pilot power in 10 s windows, alert on a sustained +2 dB
rising trend → THEN spend the SDR on full decode dwells. The ambush
stops being an alarm clock and becomes a tripwire.

## P4. Beacon proxies — other people's transmitters as free sensors
**Physics.** Propagation enhancement is broadband: a duct that lifts
Baltimore's RF21 also lifts Baltimore's FM stations. Every strong FM
carrier is a free, always-on sounding of a specific path.
**Our evidence.** Radio Tuna's band survey (53 stations = 53 paths);
FM-sweep-as-chain-control law; RF21's day/night seesaw.
**Build.** A 30 s FM strength sweep (already written for the radio
panel) tagged by transmitter city → path-enhancement scores DC /
Baltimore / Fredericksburg → prioritize TV hunts on the hottest path.
Radio Tuna literally becomes TV Tuna's propagation oracle — the two
projects close the loop.

## P5. True diversity combining (the RSPduo endgame)
**Physics.** Selection diversity (ours today) takes the better antenna:
gain = max. Maximal-ratio combining ADDS aligned signals: +3 dB at
equal SNR, and vastly more when fading is uncorrelated (rabbit ears and
discone fade independently — we've watched them trade RF7 all day).
**Build now (software, single SDR):** packet-level time diversity — the
multi-pass rescue (E7): same broadcast decoded in overlapping passes,
good packets voted/merged. **Build later (hardware):** RSPduo = two
coherent tuners = real MRC; also enables interference nulling
(subtract a reference antenna pointed at the noise).

## P6. λ/2 micro-positioning (Fresnel physics, human-in-loop)
**Physics.** Indoor multipath forms standing waves with λ/2 period —
at RF7 (177 MHz) that's ~85 cm; at RF36 (605 MHz) ~25 cm. "Re-aiming"
an antenna indoors is mostly moving it between interference nulls and
peaks of an invisible pattern.
**Our evidence.** "Re-aimed it, hands-off" swings of several dB; the
directional antenna's 8-10 dB window ripple; marginal-antenna lore.
**Build.** Aim-assist v2: flatness tone + a spoken grid protocol
("move 20 cm left… hold") that MAPS the room's standing-wave field
once, finds the physics-optimal spot, and never asks again. One
afternoon with the human, permanent placement dividend.

## Standing insight
Levels of the adaptive method so far: gain → antenna → hour →
(tonight) filter structure. The ladder above adds: **channel tempo
(P1), atmosphere (P2), detection theory (P3), other transmitters (P4),
true combining (P5), and the room itself (P6).** The radio doesn't
just tune the signal anymore — it reads the sky, the airport, the
weather, and the walls.
