---
name: pgrep-self-match-trap
description: "Wait-loops of the form `until ! pgrep -f \"script.sh\" >/dev/null; do sleep N; done` infinite-loop on themselves. The bash that wraps the loop has \"script.sh\" in its argv, so pgrep -f matches it forever even after the real script exits. Cost a full day of Ubuntu Claude session on 2026-06-16."
metadata:
  node_type: memory
  type: feedback
  originSessionId: cross-box-recovery-2026-06-16
---

Bash pattern that DOES NOT WORK:
```bash
until ! pgrep -f "stress_ab.sh" >/dev/null; do sleep 15; done
echo "done"; cat result.out
```

Why it fails: the outer bash's argv contains the literal string `pgrep -f "stress_ab.sh"`, so when pgrep -f scans /proc, it matches the WAIT-LOOP's own parent bash. pgrep self-excludes but doesn't exclude its parent. The condition is never false → infinite loop. Tests finished hours ago, Claude stuck forever, user's display crashed (Xid 79) and they couldn't tell test had completed.

**Why:** This bit on 2026-06-16. Ubuntu Claude set up TWO of these (`stress_ab.sh` + `live_soak.sh`) to background-wait for test completion. Both completed cleanly with great results (FOLD+BLOCK_NCO ~12.6% live CPU win on 1600X, bit-identical TS). Claude never resumed. User noticed when GPU separately crashed.

**How to apply:** When writing a "wait until process X exits" loop:
- Use `pgrep -fx "exact_full_cmdline"` (exact match) when feasible — kills the substring trap.
- Or use a sentinel file the script touches on exit: `until [ -f /tmp/done_marker ]; do sleep 15; done`.
- Or capture PID at launch and `wait $PID` / `kill -0 $PID 2>/dev/null` in the loop.
- NEVER use a substring pattern that will also appear in your wait-loop's cmdline.
- If you must use pgrep -f, exclude self with `pgrep -f "X" | grep -v $$` or `pgrep -f "X" | grep -v $PPID`.

The kill recipe to unstick a victim Claude: `pkill -f 'until ! pgrep -f'` (matches the wait-loop pattern itself; safe because nothing real uses that string).

Related: [[mpv-recovery-loop-safety]] (another infinite-loop class — both about loops that need an exit condition the loop itself doesn't satisfy), [[ubuntu-rig-workflow]].
