---
name: user-pref-autonomous-agents
description: "User actively likes delegating long-running test + improvement work to subagents, with verification/revert safeguards rather than restrictive scopes."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 5e2ec3a5-5dce-46ba-9541-4f3e423c8039
---

User repeatedly chose autonomous delegation in this project. When offered a multi-step test sweep, they said "run it as long as it takes". When offered overnight work, they wanted BOTH a recording bot AND a code-improvement agent in parallel.

When given restrictive safeguards on the code agent (a long "NEVER TOUCH" list), they explicitly said: *"the bot can touch those things just make sure if it breaks something to be able to detect that it's broken and fix it don't accidentally commit to bad code that's what the safeguards for it doesn't have to be that strict"*.

**Why:** They trust verification protocols (syntax check + import check + CLI smoke test + commit-only-if-passes + revert-on-failure) more than they trust file-restriction allowlists. Their model: an agent is OK to touch hard parts of the codebase if it can prove the edit doesn't break anything.

**How to apply:**
- Default to delegating long sequential work to subagents (test sweeps, overnight cleanup, multi-file refactors).
- Build verification + revert into the agent prompt, not file restrictions.
- Always isolate agent commits to a side branch and forbid push — that's the floor safeguard.
- When the user offers "you can take as long as you need", trust it; don't artificially time-box.
- Per-edit atomicity matters: ONE conceptual change per commit so a later `git revert <sha>` is clean.

Related: [[user-role]].
