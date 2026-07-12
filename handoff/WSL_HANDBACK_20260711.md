# WSL → Windows handback — 2026-07-11 night

*For the Windows session (and any box absorbing `wsl-port`): what changed
in SHARED code tonight, what was validated where, and what still needs a
Windows-side test before it's trusted there.*

## Validated on WSL (live, user-confirmed)
- **Feature governor** (`tv_tuna_panel.py`, `tv_tuner.py` CHAIN_DEFAULTS):
  non-Windows machines default to the lean chain profile
  (`STVT_CHAIN_PROFILE=full|lean` overrides). Windows behavior unchanged
  (`IS_WIN` → full). Measured on WSL: full dialect = 10,728 source
  overflows / 1.15% loss; lean = 0 / 0.000%. The 35 s IQ ring alone kept
  the chain overflowing (disk I/O vs the TCP sample stream).
- **Scanner strong-pilot rescue** (`tv_tuner.py`): sniff heuristics no
  longer veto carriers whose pilot clears the strict SNR bar — the demod
  gets the final word. Took the same antenna from 3 locks to 8/8
  (including RF36 FOX at MER 18.8 that the sniff had rejected twice).
  **Runs on Windows too** — expect scans to attempt (and likely win) a
  few carriers the old sniff filtered. Watch scan wall-time (~+9-25 s per
  rescued carrier).
- **antenna_id upgrades** (SHARED, ⚠️ needs a Windows regression pass):
  1. **Per-port passports** (`port_refs` per profile): confirmed
     sightings store a reference print per (antenna, port); global print
     no longer cross-port EMA-blended; `resolve_pending('update')` now
     also installs the port resident. User-validated on WSL: two
     consecutive port swaps recognized correctly.
  2. **s_cable**: cable-reflection ripple (cepstrum of the detrended UHF
     response) = electrical cable length; port-independent because the
     cable travels with the antenna. Blend 25/25/30/20 when both prints
     show confident ripple, legacy 30/30/40 otherwise. Real-rig values:
     Total vision 45 ns (~5.8 m), Phillips 19 ns (~2.4 m), discone 33 ns
     (~4.2 m); repeats within ~1 ns.
  3. A ≥T_RECOGNIZED match self-confirms the port residency span (the
     ports panel used to say "no antenna recognized" while the ledger
     matched at 97%).
  **⚠️ Windows TODO before trusting there:** selftest passes (13/13) and
  the schema is backward-compatible (old ledgers just lack `port_refs`),
  but the Windows ledger has months of profiles + the per-port passports
  roadmap expected exactly this — run `antenna_id.py selftest` + one
  🪪 identify on the Windows rig and eyeball the verdicts before leaning
  on it. s_cable weighting was calibrated on 3 antennas in one market.
- **tv_watch.py**: platform layer (Linux binaries/IPC socket/wlshm),
  captions off-by-default (`--sid=no`, j toggles), **format-aware
  deinterlace** (1080-line only, via mpv IPC probe — a blanket
  deinterlace field-doubled FOX's interlace-flagged 720p60 to 120 fps
  and choked the software VO). Windows path untouched.
- **Panel per-antenna scan memory**: scans stash under the recognized
  antenna's name (`lab/scans/`), 🗂 row above the grid toggles the guide
  between antennas without rescanning.

## Broadcaster fact worth keeping
RF34 program 3's 19:00 syndicated show aired GARBLED captions (proven in
the raw mux — "PORTIA4093H@" letter-salad). If caption complaints come
in, decode the raw mux before blaming the pipeline.

## Next mission (in flight): Raspberry Pi
Pi port begins from `wsl-port` lineage (governor already born — the Pi
is its primary beneficiary). Pi 4 law stands: live local decode is
hopeless (~30% real-time); roles = SoapyRemote IQ server + all-on-Pi
DVR. Validation ladder: build → selftests → (optional) replay gate →
role-appropriate live test, per the five laws.
