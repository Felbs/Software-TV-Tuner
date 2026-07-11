---
name: dont-hammer-chain-cpu
description: "Don't run frequent ffprobe / PID-sampling / multiple monitors while the live chain is running on a CPU-constrained box — diagnostic load steals cycles from the single-thread matched filter and CAUSES droughts. Less applicable on Threadripper but still a discipline."
metadata:
  node_type: memory
  type: feedback
  originSessionId: ubuntu-mempalace-import-2026-06-13
---

When the live ATSC chain is running, **diagnostic load is not free**. On a
CPU-constrained box it can be the thing that breaks the decode.

**Why:** the chain is gated by ONE thread (the matched-filter pfb_arb_resampler
at ~98% of a core, whole chain ~430% = ~4.3 cores on the Ubuntu Ryzen 1600X).
Zero headroom there. Frequent `tail -c 2MB live.ts | python3 count-PIDs`,
repeated `ffprobe`, and multiple background Monitors all steal cycles → the
matched filter falls behind → OsO → noise drought. 2026-06-01 on Ubuntu the
drought interval COLLAPSED 50min → 21min → 2min while diagnostic hammering
ran; stabilized once the checks backed off and stvt_run owned the periodic
lightweight check.

**Windows Threadripper context:** with 64 threads and ~5-6 chain cores, this
is FAR less of a hazard — there's spare CPU. But the discipline still matters
for two reasons: (1) the per-core bottleneck still exists even with idle other
cores; (2) ffprobe on a growing TS can hold file locks / cache lines that hurt
the chain's own writes.

**How to apply:** once live decode is up and verified, check health SPARINGLY
(every few minutes, not every cycle). Prefer reading the chain's own log
(`grep OsO ... .log`) over re-sampling live.ts. Let
[[linux-chain-stability-recipe]]'s stvt_run watchdog own the periodic check.
Don't stack extra Monitor tool calls on top of it. If you must debug heavily,
expect it to induce some chain stress and don't mistake that for a chain bug.

Related: [[x86-cpu-savings-ceiling]], [[user-pref-autonomous-agents]] (the
"don't break things" half of the autonomy rule).
