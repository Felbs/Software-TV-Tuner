---
name: ubuntu-rig-workflow
description: How Windows ↔ Ubuntu sync works for the TV tuner; rules to avoid clobbering Linux work
metadata: 
  node_type: memory
  type: project
  originSessionId: 5e2ec3a5-5dce-46ba-9541-4f3e423c8039
---

The user runs two physical machines:

- **Windows** (this harness, `Z:\src\magic-tv-decoder`) — development + Windows-side tv_tuner usage
- **Ubuntu rig** (`~/Software-TV-Tuner/`) — live SDR capture with RSPdx, runs the autonomous quality tuner, source of the new GR-atscplus blocks (`atsc_sync_soft`, `atsc_equalizer_pilot_dd`, `atsc_equalizer_pilot_multifs_dd`, etc.)

**Cross-machine sync is git-only.** There's no SSH from one Claude session to the other. The two machines coordinate via dedicated branches on `github.com/Felbs/Software-TV-Tuner`:

- `main` — Windows branch (don't touch from Linux flows)
- `linux-port-stvt-v2` — REPURPOSED 2026-06-01 to be the WSL-Ubuntu-on-Windows branch
- `linux-port-stvt-v3` — current Ubuntu-rig branch (created 2026-06-01 at f922e71, README + Linux live-HD recipe)
- Linux commits are authored by `Felbs` or by `dzsc9c5zvv-hub` (the Ubuntu Claude agent identity) and pushed up.
- Windows can fetch them but should NOT force-push `main` over any linux branch — that's clobbering Ubuntu's work.

**Why:** on 2026-05-18 the user asked me to "push the linux port" while Windows main was 5 commits *behind* origin/linux-port-stvt-v2, with a stale uncommitted tree that matched a commit already pushed AND already reverted on the Linux branch. A naive `git push --force` would have deleted 5 real Linux commits including the autonomous quality tuner (a2a48c9) and the 10 new ATSC primitive blocks (6fe0ebe).

**How to apply:**
- When the user says "update Ubuntu / the linux port," the answer is almost always "Ubuntu runs `git pull --ff-only origin <linux-branch>`" — not "Windows pushes anything." Give the user the exact Ubuntu-side command to run, or a paste-ready prompt for the Ubuntu Claude.
- Before any `git push` from Windows, check `git log main..origin/<linux-branch>` and the reverse. If Windows is behind, do not push without explicit confirmation that the user wants to overwrite remote.
- When pushing Ubuntu rig commits via the [[windows-relay-trick]], target the CURRENT linux branch (v3 as of 2026-06-01), not v2. The user may say "the linux branch" — confirm which version if unsure.
- The user's local Windows working tree on `main` may contain stale leftovers (e.g., the multi-FS pilot DD switch they drafted before Ubuntu took over that work). Don't auto-commit it just because it's dirty.

See also [[project-topology]].
