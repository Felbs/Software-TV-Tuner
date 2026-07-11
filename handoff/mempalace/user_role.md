---
name: user-role
description: User identity and rough project context
metadata: 
  node_type: memory
  type: user
  originSessionId: 5e2ec3a5-5dce-46ba-9541-4f3e423c8039
---

User: **Felbs** [redacted-email] per harness context; matches GitHub author on commits to `github.com/Felbs/Software-TV-Tuner`).

Working on an autonomous SDR / ATSC TV-tuner rig:
- Hardware: SDRplay RSPdx receiver, both a Windows box and an Ubuntu rig with the SDR attached
- Stack: GNU Radio + Soapy + a forked `gr-atscplus` module they've been hacking on at the C++ level (DLL/.so rebuilt frequently)
- Tooling: Python CLIs in `Z:\src\magic-tv-decoder\tools\` (`tv_tuner.py`, `tv_live.py`, etc.), plus a separate snapshot of helpers + extensive tier-log markdown files in `Z:\SDR_Agent_v2`

Style of work observed:
- Iterates aggressively at the C++ block level (Tier-1 through Tier-21+ log files document the journey), then validates with live SDR captures.
- Comfortable with deep-DSP terminology (LMS, Viterbi, FPLL, equalizer adaptation, decision-directed, field sync, PN511/PN63 correlation).
- Uses Claude Code on both machines and expects them to coordinate via git, not directly.
- Sometimes types under pressure (typos like "puch", "othere compouter", "is any of our project in this folder") — read past surface errors to the intent. Don't nitpick spelling.
- Asked for "no clarifying questions" mode but also said things that genuinely need clarification (resuming with no memory, ambiguous push intent). Ask focused, multi-option questions only when a wrong action is destructive; otherwise pick the safe reasonable interpretation and proceed.
