---
name: handoff-doc-convention
description: Cross-machine moves of STVT are documented in HANDOFF.md at the repo root — check it first before building
metadata: 
  node_type: memory
  type: project
  originSessionId: 5e2ec3a5-5dce-46ba-9541-4f3e423c8039
---

When STVT moves between machines (Ubuntu rig → Windows/WSL, or onto a fresh box), the source-of-truth setup doc is `HANDOFF.md` at the root of the `linux-port-stvt-v2` branch.

Confirmed present on 2026-05-31, dated `2026-05-31`. Covers:
- Build & install steps (`cd gr-atscplus/build && cmake .. && make -j$(nproc) && sudo make install`)
- The CPU-specific `.so` rule — **never copy the built `.so` between machines, always rebuild on the target**
- The "winning config" env vars (STVT_RS, STVT_VITERBI, STVT_EQ, STVT_SPS, STVT_RRC_SYMS, STVT_TEISCRUB, STVT_IFGR, STVT_RFGAIN_SEL, STVT_ANTENNA) for the Ryzen 1600X rig
- Fast-hardware quality bump: `STVT_SPS=1.5 STVT_RRC_SYMS=8 STVT_TEISCRUB=1` (full stock)
- Verification metric: `grep -c OsO tools/data/tv_live/tv_tuner.tv_live.log` — should be near zero on fast hardware
- Diagnostic tooling: `tools/record_iq.py`, `tools/tv_replay.py` (deterministic offline replay — the way to separate software vs RF problems)

**Known gaps in HANDOFF.md** (as of 2026-05-31) — both reference external files that aren't committed:
- The SoapySDRPlay3 ring-buffer patch (262144×32 in `SoapySDRPlay.hpp`) — referenced as "see memory," patch itself not in repo
- `~/fix_linux_tuning.sh` (CPU governor script run once per boot) — referenced but not in repo

**Why:** the user (Felbs) actively moves between an Ubuntu SDR rig and a Threadripper Windows/WSL setup. Ubuntu Claude wrote HANDOFF.md as a deliberate handoff artifact; the user has explicitly said they want this kind of instruction on GitHub so cross-machine moves don't need agent-to-agent telephone.

**How to apply:** when the user says "get STVT running on machine X" / "move it to Y" / "build on the new box," read HANDOFF.md first and follow it verbatim. If you discover something HANDOFF.md is missing (e.g. the SoapySDRPlay3 patch contents, or environment-specific notes about WSL/usbipd-win), update HANDOFF.md as part of the work — that's the durable home for that knowledge, not memory or chat.

See also [[ubuntu-rig-workflow]], [[project-topology]], [[windows-relay-trick]].
