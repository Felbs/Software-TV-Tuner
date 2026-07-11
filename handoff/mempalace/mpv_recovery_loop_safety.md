---
name: mpv-recovery-loop-safety
description: "Any GPU-process recovery loop (mpv, ffplay, vlc respawn) needs max-retries + a success condition. 2026-05-29 an unbounded mpv probe-stuck respawn loop on Ubuntu took the GPU off the PCIe bus (Xid 79). Same crash class on Windows would kill the desktop GPU."
metadata:
  node_type: memory
  type: project
  originSessionId: ubuntu-mempalace-import-2026-06-13
---

**Rule:** when writing any auto-recovery loop that respawns a GPU-using
process (mpv, ffplay, VLC, or anything that hits NVIDIA/AMD GPU drivers),
you MUST bound it with `max_retries` AND check for a success condition
between attempts. Unbounded respawn is a system-stability bug.

**The Ubuntu incident (2026-05-29):** `tv_tuner.py` had a recovery loop that
killed mpv whenever it appeared "stuck during probe" and respawned it. When
the chain output was bad enough that EVERY mpv instance got stuck, the loop
respawned forever — eventually triggering **Xid 79 (GPU off PCIe bus)**. The
NVIDIA driver removed the device. Required a full reboot to recover. Logged
in `mpv_recovery_killed_gpu.md` on Ubuntu mempalace; tv_tuner.py v2 fix
added bounded retries + a "saw decode progress" gate.

**Why this matters on Windows:** the user's Threadripper rig drives display
output through a GPU; a runaway mpv-respawn loop in any Windows tool would
trigger the same hardware-removal class of bug. Windows actually handles
TDR (Timeout Detection & Recovery) better than Linux but unbounded GPU
process churn is still a bad-idea recipe.

**How to apply:** any time you add or review a recovery loop in tv_tuner.py /
stvt_run.sh / stvt_play_hd.sh / any future supervisor script — check that
it has:
1. A retry counter with a hard cap (e.g. `MAX_RESTARTS=30`).
2. A cooldown between retries (e.g. `sleep 5`).
3. A "stop trying" condition based on observable success, not just
   "process exited" (e.g. "saw N frames decoded" or "TS file grew").
4. A clear error exit when the cap is hit, not silent loop continuation.

Existing Windows tools follow this pattern (stvt_run.sh has
`MAX_CHAIN_RESTARTS=30` + cooldown). Don't regress it.

Related: [[dont-hammer-chain-cpu]], [[linux-chain-stability-recipe]]
(stvt_run watchdog patterns), [[user-pref-autonomous-agents]] (the
verification half of the autonomy rule).
