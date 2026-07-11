---
name: sdr-single-tuner-rule
description: SDRplay RSPdx is single-tenant — only one tv_live can hold it at a time. The scheduler must SKIP not STACK same-mux recordings; multirec can fan out within one mux but never across.
metadata: 
  node_type: memory
  type: project
  originSessionId: 5e2ec3a5-5dce-46ba-9541-4f3e423c8039
---

Hard hardware limit on this rig:

- RSPdx holds ONE tuning at a time (~10 MHz of contiguous IQ, fits one 6 MHz ATSC mux + guard).
- Inside one mux, multirec demuxes all programs simultaneously with ffmpeg -map per-program. Proven up to 8 programs in DC (RF 35 ION mux: 19.39 Mbit/s aggregate — the ATSC spec ceiling).
- ACROSS muxes: only one at a time. To record NBC + Fox simultaneously you'd need a second SDR.

Scheduler enforcement (stvt_schedule.py daemon, `cmd_run`): when about to fire an entry while another recording is active, it SKIPS the new one with a status like `skipped: same-mux RF36 record already active ("...");  use rmux to cover both` or `skipped: different-mux ... only one SDR`.

Earlier bug: daemon used to skip only different-RF conflicts. Same-RF would spawn a second multirec → second tv_live → second SDR open → fail → multirec bailed at ~30s with exit 0, producing a misleading "done" stub file with only seconds of content. Fixed: any concurrent multirec attempt now SKIPS, not stacks.

User-facing implication: if you want multiple shows on the SAME mux, use `rmux <#>` or `rrf <RF>` in the `tv` controller — one multirec captures all programs in that mux. Don't queue two separate single-program records on the same mux during overlapping windows.

**Why:** Two days of debugging concurrency stubs. The bug looked like a daemon success because exit was 0 and queue marked "done", but the recording was only seconds long.

**How to apply:** Any time the user asks "can I record A and B simultaneously?", check whether A and B share an RF (then YES via multirec) or not (then NO, only one). [[dvr-architecture]] has the full picture.
