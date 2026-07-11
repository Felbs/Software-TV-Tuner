# 🪪 H(f) PRINT — antenna auto-identification: design, real-data validation, wiring

2026-07-11 · module: `antenna_id.py` · ledger: `lab/antenna_profiles.json`
· panel: 🪪 H(f) PRINT button + scan-byproduct harvest in `tv_tuna_panel.py`

**The name (user asked for math):** every antenna+cable system is a
filter with a transfer function **H(f)**; the sweep estimates its
magnitude shape |H(f)| across the market's frequencies (level above own
noise floor, pilot margins, carrier strengths). Absolute level drifts
with propagation; the SHAPE is the antenna — so the print IS |H(f)|,
and the UI calls it that.

**The vision (user's):** plug in any antenna and go. The rig fingerprints
whatever is on the port, recognizes antennas it has met before, notices
when the hardware changed, and starts/attaches Knob-of-Time history
automatically — knowledge follows the physical antenna, not the port.

## 1. The signature

Every full scan's phase-1 power sniff already measures the antenna+cable
system at all 35 market frequencies: RMS level, pilot SNR, pilot
sharpness. That IS a gain-vs-frequency fingerprint, and it is free — no
extra radio time. `signature_from_scan()` keeps only RELATIVE quantities:

- `resp` — per-frequency RMS in dB **above the sweep's own noise floor**
- `pilot` — pilot SNR (a ratio by construction), clipped to [−5, 70]
- `carriers` — frequencies with pilot ≥ 30 dB (the scanner's strict gate)

Absolute levels swing ±3–7 dB with diurnal propagation (documented in
`antenna_fingerprint_research.md`); shapes and ratios are the stable
part, so similarity compares only differences-of-shape:

- `s_level` — MAD spread of the per-frequency `resp` difference on
  informative frequencies (either sweep > 4 dB above floor). A constant
  offset (inline amp) cancels; the residual is the antenna.
- `s_pilot` — same on the pilot vector (either ≥ 15 dB).
- `s_carrier` — union-weighted agreement of continuous carrier strength
  (ramp 20→40 dB, so a 30 dB gate flicker can't flip membership).
- score = 0.30·s_level + 0.30·s_pilot + 0.40·s_carrier;
  knees: 2 dB (level) / 4 dB (pilot) spread → 0.5 similarity.

Dead channels are kept in the sweep (they anchor the shape) but are
excluded from voting — two sweeps always agree that RF33 is empty, and
that must not count as evidence of identity. A flat sweep (< 4 dB spread,
no carrier ≥ 30) is refused outright: fingerprinting a wedged radio would
poison the ledger.

**Bug found while building:** strict-carrier scan records (the lock-test
results — RF31/34/35/36 on today's scans) carry **no `freq_mhz` field**.
Any consumer keying on `freq_mhz` silently drops the four most
informative channels. `rf_to_mhz()` reconstructs frequency from the RF
number.

## 2. Validation on real saved sweeps — the go/no-go matrix

Full phase-1 sweeps that survived on disk (scan.json is overwritten per
scan; only the last pair plus quarantined files remain), **plus** the
June-era IQ fixture set (`tools/scan_lab/fixtures/*.cf32`, 35 real
captures re-analyzed offline through `sdr_sweep._analyze` — a genuinely
different antenna/rig-era, zero radio time):

| pair | score | verdict | level spread | pilot spread | s_carrier |
|---|---|---|---|---|---|
| shed 13:07 × shed 12:36 (SAME antenna, 31 min apart) | **0.906** | SAME | 0.48 dB | 1.5 dB | 0.90 |
| shed 13:07 × discone dud 00:49 | 0.588 | DRIFT? (asks) | 1.8 dB | 5.0 dB | 0.76 |
| shed 12:36 × discone dud 00:49 | 0.590 | DRIFT? (asks) | 2.0 dB | 4.4 dB | 0.76 |
| shed 13:07 × fixture rig (June) | 0.456 | DIFFERENT | 5.5 dB | 3.8 dB | 0.66 |
| shed 12:36 × fixture rig (June) | 0.418 | DIFFERENT | 6.3 dB | 4.6 dB | 0.65 |
| discone dud × fixture rig | 0.395 | DIFFERENT | 4.3 dB | 4.5 dB | 0.52 |
| antenna-C scan 06-20 | — | UNUSABLE (flat — correctly refused) | | | |

Thresholds: RECOGNIZED ≥ 0.75, CHANGED ≥ 0.55, else NEW.
Margins: same-antenna 0.906 vs best cross-antenna 0.59 — and that 0.59
is the panel's own quarantined no-lock dud, which lands in the ASK zone
(the system questions it instead of silently misfiling). Every clean
cross-antenna pair scores ≤ 0.46.

### The three port-A residents of today (honest caveat)

Old Faithful (10:52), Total Vision (11:07) and shed directional (12:32+)
all lived on port A today, but their full phase-1 sweeps were
**overwritten** — only the shed pair survived. A coarse proxy from
`quality_history.csv` scan rows (locked-RF set + decode-MER shape,
6 dims vs the real signature's 70):

| pair | proxy score | lock-set jaccard | MER spread |
|---|---|---|---|
| shed 12:32 × shed 13:02 (same) | **0.98** | 1.00 | 0.3 dB |
| Old Faithful × shed | 0.28 | 0.12 | — |
| Total Vision × shed | 0.35 | 0.25 | — |
| Old Faithful × Total Vision | 0.74 | 0.83 | 1.2 dB |
| Philips-B × shed | 0.58–0.62 | 0.38 | 0.2–0.5 dB |

Verdict: shed separates cleanly from both UHF antennas even on 6 dims
(it is VHF-strong/UHF-hi-deaf — lock sets barely overlap). Old Faithful
vs Total Vision are near-neighbors (both UHF antennas on the same
towers); the proxy puts them at 0.74 = the ASK zone. The full
35-frequency signature sees what the proxy can't (RF31 margin flips
15.7↔18.9, RF15 presence), but **that pair is unverified on full
signatures** — the data no longer exists. Mitigation shipped: every scan
now archives its signature into the ledger automatically, so identity
data can never be overwritten again.

## 3. What ships (wired today)

- **`antenna_id.py`** — standalone, pure stdlib (numpy only via
  sdr_sweep subprocess). 13-check selftest (temp store, synthetic
  antennas; never touches the real ledger). CLI: `selftest | matrix
  FILE... | observe [SCANFILE] | report`.
- **Ledger** `lab/antenna_profiles.json` — profiles are DATA: signature,
  friendly name, first/last seen, match count, port history spans
  (append-only; forks archive, nothing destroyed). Bootstrapped today
  from the two saved shed sweeps: `ant-0001 'shed directional'`,
  enrolled at 12:36, re-recognized at 13:07 with **90 % match**.
- **Verdicts** (`observe()`):
  - `RECOGNIZED` — same profile on the port; EMA-refresh (α=0.25) so
    cable aging tracks without one weird sweep hijacking identity.
  - `MOVED` — a known profile reappeared on a different port: port gets
    a fresh epoch (resident changed), profile keeps its accumulated
    history; UI asks to confirm the attach ("this looks like the antenna
    you called 'shed directional' (92 % match)").
  - `CHANGED` — gray zone (0.55–0.75): a pending question, **never a
    silent fork**. UI confirm: OK = update profile, Cancel = fork new.
  - `NEW` — no match: auto-profile + auto-epoch, exactly like 🔌.
  - `ADOPTED` — bootstrap on a virgin port: enroll WITHOUT resetting
    learning (first deployment must not nuke existing knob history).
- **Panel wiring** (`tv_tuna_panel.py`):
  - scan-byproduct: every successful scan fingerprints for free
    (`observe_scan` at scan end; dud-restored scans skipped).
  - 🪪 IDENTIFY button → `/api/identify` — ~60 s Welch-averaged
    `sdr_sweep.py` pass over the market grid (radio must be idle;
    waterfall pauses via the IDENT flag like SCAN/FLAT/BAL do).
  - `/api/antenna_id/resolve` (update|fork), `/api/antenna_id/attach`
    (confirm a MOVED span), `/api/antenna_id/name` (friendly name).
  - status carries `antid` {seq, event, busy, line}; the JS toasts each
    new event once and drives the confirm/prompt flows.
  - epoch machinery factored into `fresh_epoch()` — one code path for
    the 🔌 button and every automatic verdict.
- **Knob-of-Time linkage**: `rows_for_profile(pid, rows)` selects
  quality-history rows inside the profile's confirmed port/time spans —
  the antenna's education follows the metal across ports. (Data model +
  API shipped; surfacing profile-backed curves in the NERD cards is
  future work.)

## 4. Live verification (2026-07-11) — and two real bugs it caught

- Panel restarted with the new wiring: single instance, HTTP 200,
  STVT_PERSIST_RETUNE=1, logs → `lab/panel_20260711_*.log`.
- Live 🪪 IDENTIFY on Antenna A was exercised end-to-end through the
  software stack: route accepted → IDENT flag parked the waterfall
  ("🪪 antenna identify in progress — sweep paused") → `sdr_sweep.py`
  subprocess spawned → **SDR open failed: the RSPdx is off the USB bus**
  (`Get-PnpDevice -PresentOnly` shows 0 present RSPdx; every instance
  "Unknown"). SDRplayAPIService restart did not bring it back — per the
  standing law, stuck SDR firmware needs a **physical USB replug** (it
  may also simply be unplugged from the PC move). First 🪪 IDENTIFY or
  📡 SCAN after the replug completes the live validation.
- **Bug 1 (fixed):** `identify_sweep()` originally passed raw
  `os.environ` to `sdr_sweep.py` — without the SDRplay API DLL dir on
  PATH, SoapySDR loads **no sdrplay module at all** and enumerates an
  empty device list ("Device::make() no match"). Now defaults to
  `tv_tuner.env_with_sdrplay()`.
- **Bug 2 (launch recipe, documented):** relaunching the panel from a
  bare shell with the full python.exe path but WITHOUT radioconda
  activation leaves `radioconda\Library\bin` (and the SDRplay API dir)
  off PATH — the panel's own in-process waterfall then can't load the
  sdrplay module either, which looks exactly like a missing radio.
  The relaunch recipe must prepend:
  `C:\Users\user\radioconda;C:\Users\user\radioconda\Library\bin;`
  `C:\Users\user\radioconda\Scripts;C:\Program Files\SDRplay\API\x64`.
  The panel now runs with that PATH; waterfall honestly reports
  "radio busy — waiting" until the SDR returns.
- **After the replug (user: "shed antenna back on"), the live loop
  CLOSED:** 🪪 H(f) PRINT on Antenna A →
  **"recognized shed directional on Antenna A (90 % match)"**
  (score 0.897 — level spread 0.30 dB, pilot spread 1.2 dB, s_carrier
  0.82 over 31 shared frequencies). Note the cross-instrument win: the
  profile was enrolled from the scanner's 0.1 s phase-1 sniffs, and the
  live print used the standalone 1.5 s Welch-averaged sweep — the
  |H(f)| shape survived a different dwell, different code path, and a
  physical unplug/replug of the radio.

## 5. Future (deliberately not built today)

- Panel-startup auto-identify when radio is idle (design allows it —
  one `ident_run()` call gated on `radio_busy()`; left off until the
  toast UX has a few days of scan-byproduct mileage).
- Profile-backed NERD cards / guide badges ("best hours for THIS
  antenna, wherever it's plugged in") via `rows_for_profile`.
- Merging pre-identification epochs into profiles retroactively.
- Cross-market universality test (signatures reference only the local
  scan grid; a stranger's market works from zero by construction).

## Laws honored

No antenna names/characteristics in code (the ledger is data; the one
name in it was typed by a human today). Works from zero on a fresh
install (empty ledger → ADOPTED). Nothing destroyed (CHANGED asks;
forks keep the old profile; epochs archive). Radio courtesy: the only
new radio path is the explicit IDENTIFY button; scans fingerprint for
free.
