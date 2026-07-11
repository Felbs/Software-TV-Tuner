---
name: user-pref-just-execute
description: "User wants me to execute commands directly rather than show shell snippets for them to copy-paste. They have the tools; I should use them. 'You should just do it all for me' — 2026-06-20."
metadata:
  node_type: memory
  type: feedback
  originSessionId: ai-ham-2026-06-20
---

When the work needs a command run, **run it** — don't post a `bash` code block for the user to copy. They have Bash/PowerShell tools and explicitly prefer I take control. "Better yet instead of me copy and pasting you should just do it all for me that way you can take better control."

**Why:** 2026-06-20 evening, AI ham project. I ended a turn with `python ai_ham.py ...` snippets and asked them to launch. They corrected: just do it. This builds on [[user-pref-autonomous-agents]] (they like autonomous delegation with verification/revert safeguards). The pattern: execute, watch the output, report what happened — don't punt to the user.

**How to apply:**
- Long-running tools? Launch with `run_in_background: true`, monitor via Read on the output file, summarize findings.
- Mutually-exclusive resources (SDR is single-tenant)? Stop the conflicting process myself with TaskStop, don't ask permission to do the obvious cleanup.
- Wait-and-report patterns? Use them. If the user wants progress, they'll ask — give them rolling status.
- Exception: ASK before genuinely destructive actions (git reset --hard, force-push, rm -rf on uncertain paths, mass-rename). The general "do the operational legwork" rule doesn't override the existing destructive-action guard.
- Posting commands as documentation/reference is still fine (e.g. in a README, or as "next time you can also run X"). The rule is about *what you do this turn*, not about reference material.

Related: [[user-pref-autonomous-agents]] (broader autonomy preference with verify/revert), [[user-role]].
