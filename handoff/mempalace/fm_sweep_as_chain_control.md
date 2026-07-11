---
name: fm-sweep-as-chain-control
description: "When an ATSC TV scan returns zero carriers, sweep the FM broadcast band (88-108 MHz) as a CONTROL — it proves whether the SDR + antenna + port + chain are alive without swapping antennas physically. Confirmed 2026-06-20 on Windows."
metadata:
  node_type: memory
  type: feedback
  originSessionId: windows-discone-fm-sweep-2026-06-20
---

When a `python tools/tv_tuner.py --scan` returns **0/35 channels + 0 weak/intl/atsc3 carriers**, don't immediately assume the SDR or chain is dead — do an FM-band power sweep on the same antenna+port first. FM stations are guaranteed to be there (every metro area has multiple 100 kW transmitters within 30 mi) so they're the perfect "is anything getting through?" control.

**Why:** 2026-06-20 the user moved the SDR back to Windows after the backyard Ubuntu antenna tests and asked to decode TV from the discone on port C. ATSC scan = 0 carriers, even with `--thorough` + max gain. Without a control I'd have been left guessing between "SDR broken / port C broken / antenna broken / chain broken / signal just absent." The FM sweep returned 12/12 DC stations with 10 STRONG (+25-31 dB SNR), proving the SDR/port/antenna/chain were all fine — the discone+port-C combo just can't deliver UHF ATSC due to HF-port frequency rolloff (port C is HF-optimized, falls off above ~30-100 MHz) + discone polarization mismatch (vertical vs ATSC horizontal = 15-20 dB loss). Two compounding physics issues → 0 carriers, not a fault.

**How to apply:** when a TV scan turns up empty AND the user wants to know "is it the chain or the antenna," run the FM control sweep first. The pattern is at `Z:\SDR_Agent_v2\pending\fm_sweep.py` (or re-derive: SoapySDR `Device("driver=sdrplay")`, set antenna, sweep ~12 known local FM frequencies + 4-5 quiet refs between bands, measure RMS power, compare). **Critical:** start with LOW gain (`IFGR=50`, `rfgain_sel=6`) — FM signals are huge, max gain (`IFGR=20`, `rfgain_sel=0`) drives the ADC into saturation and every FM frequency reads identical -5 dBFS (false negative). If FM works at the right gain, the answer is physics (antenna/port/polarization), not software/hardware.

**Useful quiet reference frequencies** (away from broadcast/aircraft/marine/weather/cell): 75, 138, 155, 170, 220 MHz. AVOID 162 MHz (NOAA weather radio is loud there) and 108.3 MHz (FM-adjacent spillover).

**Gain semantics reminder** (per `tools/tv_live.py`):
- `IFGR` is *gain reduction* — higher number = less gain (range 20-59).
- `rfgain_sel` is set via `writeSetting("rfgain_sel", str(N))`, NOT `setGain` — driver clamps `setGain("RFGR", N)` silently.
- Disable AGC first: `setGainMode(SOAPY_SDR_RX, 0, False)`.

Related: [[shed-antenna-offline-proof-2026-06-20]] (Ubuntu's "antenna is the problem, not code/cable/amp" parallel — backyard antenna failed for the same physics class of reason), [[scanner-chain-env-gotcha]] (scanner env-var pattern), [[noise-fingerprint-check]] (the "is the chain alive?" sibling check, on output side).
