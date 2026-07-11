---
name: scanner-chain-env-gotcha
description: "tv_tuner.py scanner must pass the same STVT_* chain env vars the play path uses, or it locks far fewer channels than the rig can actually tune."
metadata: 
  node_type: memory
  type: project
  originSessionId: 5e2ec3a5-5dce-46ba-9541-4f3e423c8039
---

`tv_tuner.py --scan` and the play path (`stvt_channel_step.py`, multirec) BOTH spawn `tv_live.py`, but they used to pass different env. The play path injects a CHAIN_DEFAULTS dict (STVT_EQ=long, STVT_VITERBI=soft, STVT_EQ_LKG=1, STVT_IFGR=45, STVT_RFGAIN_SEL=3, STVT_ANTENNA="Antenna A", STVT_SPS=1.1, STVT_RRC_SYMS=8, STVT_TEISCRUB=1). Without those, the scanner uses defaults and the equalizer cold-start probabilistically misses marginal carriers.

`env_with_sdrplay()` in tv_tuner.py now bakes in CHAIN_DEFAULTS so both paths get them.

Symptom of the bug: scan locks 4/10 channels even though picker tunes those same channels fine. "in_rms=420" (10x normal) was the visible giveaway because RFGAIN_SEL fell back to its hot default.

Also relevant on the same fix path: `viterbi="stock"` in spawn_tv_live routes to tv_live.py (which honors STVT_VITERBI=soft from env). `viterbi="soft"` routes to tv_live_softvit.py which is an older fork that IGNORES env entirely. Use stock + STVT_VITERBI=soft, never the softvit path.

**Why:** Two-and-a-half-hour debug to find it. The bug is invisible from any single file because the gap is between two callers of spawn_tv_live.

**How to apply:** Any time the scanner is missing channels the picker can tune: first verify env_with_sdrplay() is still injecting CHAIN_DEFAULTS, and the scanner's scan_one_rf_with_retry call still passes `viterbi="stock"` (NOT "soft"). Don't introduce a new tv_live variant that doesn't read env.
