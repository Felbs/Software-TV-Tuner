---
name: project-topology
description: Where the Software-TV-Tuner code actually lives — cwd is misleading
metadata: 
  node_type: memory
  type: project
  originSessionId: 5e2ec3a5-5dce-46ba-9541-4f3e423c8039
---

The harness cwd is `Z:\SDR_Agent_v2`, but that folder is **not a git repo** and is a stale snapshot (last touched 2026-05-06). The actual working repo is:

- Path: `Z:\src\magic-tv-decoder\`
- Remote: `https://github.com/Felbs/Software-TV-Tuner.git`
- Tools dir: `Z:\src\magic-tv-decoder\tools\` (tv_tuner.py, tv_live.py, atsc_psip.py, atsc_cc.py, default_stations.py, tv_player.py — the same names exist in both folders but `Z:\src\magic-tv-decoder\tools\` has the newer copies)
- gr-atscplus C++ source: `Z:\src\magic-tv-decoder\gr-atscplus\lib\` (atsc_fs_checker_inst_impl.cc with the Tier-21 spacing validator, the new pilot_dd/pilot_multifs blocks, etc.)

Branches on origin (as of 2026-05-18):
- `main` — Windows working branch, stale relative to linux work
- `linux-port-stvt-v2` — active Ubuntu-rig branch, source of truth for the Linux deployment
- `stvt-best-build` — older snapshot

**Why:** the agent that built this v2 folder dumped a copy of the TV tuner tools into `Z:\SDR_Agent_v2\` for local convenience, but the real version control + active work lives in `Z:\src\magic-tv-decoder`. Don't `Edit` files in `Z:\SDR_Agent_v2\` thinking the change will land on GitHub — it won't.

**How to apply:** when the user says "the project," check `Z:\src\magic-tv-decoder` first for git state, branches, recent commits. The `Z:\SDR_Agent_v2` folder is useful for reading historical tier logs (`decoder_tier*_log.md`, `final_report.md`, `NOTIFY.txt`) but is not the place to make code changes intended to ship.

See also [[ubuntu-rig-workflow]].
