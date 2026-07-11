---
name: branch-layout
description: "Repo branch map after the 2026-07-07 GREAT CLEANUP: only 4 branches remain — `main` (= main-universal, front page, all Fable-finale work), `main-universal` (Windows dev line, where pushes go), `main-linux` (Ubuntu rig), `pi-port-stvt` (Pi rig). ALL dead branches deleted, preserved as archive/* TAGS."
metadata: 
  node_type: memory
  type: reference
  originSessionId: 368b030c-6ab2-4abc-a2a8-33ccdf8dc873
---

**2026-07-07 GREAT CLEANUP (user: "make this the main branch, stick all
the old branches together into one archive"):** dead branches tagged
`archive/<name>` then DELETED from origin: archive/windows (da25b7e),
archive/marginal-antenna-tuning (f9c9d32), archive/stvt-best-build
(2e2e563), archive/wsl-port-stvt-v2 (faa94f1), archive/main-archive
(6baaf63) — full history reachable via tags. `main` fast-forwarded to
main-universal (a7f9593, the whole Fable-finale night). LIVE branches
deliberately KEPT (not old, machines track them): `main-linux` (Ubuntu),
`pi-port-stvt` (Pi). Working flow: develop + push `main-universal`,
fast-forward `main` at milestones (push to default branch needs the
user's say-so each time).

Final layout: main (front page) · main-universal (Win dev) ·
main-linux (Ubuntu rig) · pi-port-stvt (Pi rig) · archive/* tags.

**2026-07-01 MAIN SWAP #2 (user-requested "new main, preserve all old
branches"):** `main` was FORCE-PUSHED to the universal release = `windows`
branch content + `adaptive-tv/` universal-tuner suite + rewritten README +
science.md §12.5 MER (commit e2f1fa2, also pushed as branch `main-universal`).
The Ubuntu/Linux line that had been main since 6/15 is preserved verbatim as
**`main-linux`** (a786b06). **Cross-machine impact: the Ubuntu rig's
sync-via-origin/main workflow is BROKEN by this — Ubuntu must now track
`main-linux`** (`git branch -u origin/main-linux` or re-point pulls/pushes);
same for any relay push of Ubuntu commits: target `main-linux`, NOT `main`.
Pi unaffected (`pi-port-stvt`). See [[mer-dial-universal-algorithm]] for what
shipped.

Layout after 2026-07-01:

| Branch on origin | Role | Where it runs |
|---|---|---|
| `main` | **Universal release** — windows line + adaptive-tv/ + MER docs. Default on GitHub. | Z:/src/magic-tv-decoder |
| `main-linux` | Ubuntu/Linux line (was `main` 6/15→7/01, before that `linux-port-stvt-v3`) | Ubuntu rig at /home/felbs/Software-TV-Tuner |
| `main-universal` | same commit as new `main` (safety duplicate) | — |

Older layout (2026-06-15, superseded above):

| Branch on origin | Role | Where it runs |
|---|---|---|
| `main` | **Active Linux/Ubuntu line** — was `linux-port-stvt-v3`. FPLL fold + BLOCK_NCO default-on. Default branch on GitHub. | Ubuntu rig at /home/felbs/Software-TV-Tuner |
| `windows` | Active Windows dev — DVR stack, scheduler, controller, signal meter, FPLL fold env-gated | Z:/src/magic-tv-decoder (primary worktree) |
| `pi-port-stvt` | Pi 5 forge — surfer/scanner/supervisor experiments, [[pi5-to-x86-backport]] source | radiopi @ 192.168.4.27 |
| `wsl-port-stvt-v2` | WSL port (older Linux path) | Z:/src/magic-tv-decoder-linux secondary worktree |
| `main-archive` | Frozen legacy trunk — was `main` before 2026-06-15. Predates DVR work. Kept for historical reference. | — |
| `stvt-best-build` | Older snapshot branch | — |

**2026-06-15 swap:** user asked for `linux-port-stvt-v3` → `main` and old `main` → `main-archive`. Done via GitHub branch-rename API (preserves PR redirects), then default branch flipped back to `main` (it auto-followed the rename to `main-archive`). Local remote-tracking refs auto-pruned on first `git fetch --prune` after the rename.

Cross-machine impact:
- **Ubuntu rig:** local branch is still named `linux-port-stvt-v3`. After `git fetch --prune` the upstream tracking ref vanishes. Either rename locally (`git branch -m linux-port-stvt-v3 main` + `git branch -u origin/main`) or just check out fresh `main`. Pushes that used to go to `linux-port-stvt-v3` must now go to `main`.
- **Windows:** unaffected (still on `windows` branch). For relay pushes from Ubuntu, fetch their commits as before but push to `origin/main`, not `origin/linux-port-stvt-v3`.
- **Pi:** unaffected (still on `pi-port-stvt`).

Historical naming weirdness (kept for spelunking):
- `main-catchup-from-v3` was renamed → `windows` on 2026-06-05
- `linux-port-stvt-v2` on origin was renamed → `wsl-port-stvt-v2` earlier; local worktree at `Z:/src/magic-tv-decoder-linux` may still be on the orphaned local-only `linux-port-stvt-v2` branch
- `linux-port-stvt-v3` → `main` on 2026-06-15 (this swap)

Also: the local repo's `remote.origin.fetch` refspec was scoped to ONLY `main` originally — fixed 2026-06-05 with `+refs/heads/*:refs/remotes/origin/*`. If `git fetch` shows no new branches on a different box, check that refspec first.

Related: [[ubuntu-rig-workflow]], [[windows-relay-trick]], [[no-push-linux-without-test]], [[wsl-handoff-briefing]], [[pi5-to-x86-backport]].
