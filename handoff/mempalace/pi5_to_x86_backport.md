---
name: pi5-to-x86-backport
description: "Feature flow on linux-port-stvt-v3 reversed in mid-June 2026: Pi 5 became the experimental forge, validated features get backported to x86. v3 now carries surfer/scanner/supervisor UI, FPLL fold, MIN_BUF, cs16 capture — originally Pi-side work."
metadata:
  node_type: memory
  type: project
  originSessionId: pi-ubuntu-relay-2026-06-13
---

The mental model from earlier sessions was: Windows leads → linux-port-stvt-v3
ports it → pi-port-stvt forks v3 and adds Pi-specific work. That broke as of
2026-06-13 — the user now treats **Pi 5 as the experimental forge** and ports
validated work *back* to x86 via `linux-port-stvt-v3`.

**Concrete state (origin/linux-port-stvt-v3 @ 75b7a1f, 2026-06-13):**

6 commits ahead of the prior memory snapshot (218c5e2), all sourced from
Pi 5 work:
- `f973560` — Port Pi-5 surfer/scanner/supervisor features to x86 (Phase 1)
- `08631ec` — Port FPLL fold (STVT_FPLL_FOLD) C++; bit-exact on x86, default off (Phase 2)
- `3d78167` — Port STVT_MIN_BUF / STVT_MIN_BUF_BYTES buffer knob, default off (Phase 3)
- `0b382b4` — Port cs16 IQ capture format — halves capture disk (Phase 3, DVR slice)
- `75b7a1f` — stvt_run: default STVT_FPLL_FOLD=1 on x86 — ~6% less live CPU, bit-identical
- (plus `88fbbaf` Pi 4 feasibility docs)

**Default change worth remembering: STVT_FPLL_FOLD = 1 by default on x86 now.**
Prior memory still describes it as "default off." Bit-identical to off, just
~6% less live CPU. If a future Windows session ports this commit, expect the
flip and don't treat it as a regression.

**Why:** Pi 5 hit real-time first with the fused-filter + S16 NEON equalizer
chain, and the UI work (surfer/scanner/supervisor) was easier to iterate on
the Pi where the user was actively watching TV. The user is now confident
enough in the Pi-side features to bring them back to the x86 codebase.

**How to apply:**
- When the user mentions "the Ubuntu rig" or "linux-port-stvt-v3," expect it
  to contain Pi-5-originated UI features (surfer, scanner, supervisor), not
  just the original ATSC primitive work. Don't be surprised if `tools/`
  layout suddenly mirrors the Pi.
- When relaying Ubuntu commits via [[windows-relay-trick]], read the commit
  messages — "Port X from Pi 5" is a hint that this is part of the Phase 1-3
  backport sweep, not original Ubuntu work.
- The Pi-port branch ([[pi-rig]]) is still ahead of `v3` on Pi-specific
  things (88:a2:9e Pi 5 hardware setup, ARM-only builds). Backport flow
  doesn't make v3 a superset.

Related: [[pi-rig]], [[branch-layout]], [[ubuntu-rig-workflow]],
[[windows-relay-trick]].
