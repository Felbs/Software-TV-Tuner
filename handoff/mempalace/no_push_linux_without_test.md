---
name: no-push-linux-without-test
description: "Do NOT push code to linux-port-stvt-v2 or linux-port-stvt-v3 without the user testing that branch's environment first. Windows fixes don't auto-apply to Linux."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 368b030c-6ab2-4abc-a2a8-33ccdf8dc873
---

When the user asked to "port the new Windows code to the WSL and Linux branches", they explicitly said: *"dont push anything to those branchs yet i have to test the code for each one fist"*.

**Why:** the Windows branch carries lots of Win32-isms — CREATE_NO_WINDOW flags, `wmic`/`taskkill`/PowerShell calls, hardcoded `C:\Program Files\VideoLAN\VLC\vlc.exe` paths, `os.add_dll_directory` for SDRplay, the global hotkey listener using ctypes Win32 API. Most tools have `sys.platform == "win32"` guards, but a blind merge can still ship subtle bugs on a different OS. The Ubuntu rig is the production environment for the linux-port-stvt-v3 branch; pushing untested code there can break recordings on the user's actual TV-watching hardware.

**How to apply:**
- Merge Windows commits into a Linux port branch ONLY in a worktree where they can be live-tested on that OS first.
- WSL branch (linux-port-stvt-v2) tests happen on Z:/src/magic-tv-decoder-linux worktree.
- Real-Ubuntu branch (linux-port-stvt-v3) tests happen on the Ubuntu rig — and pushes from there are subject to the [[windows-relay-trick]] (Ubuntu has no GitHub creds; Windows pushes for it).
- Combined with [[ubuntu-rig-workflow]]: never push over the Ubuntu rig's in-flight commits; fetch first to confirm what's there.

Order of operations for a port:
1. Fetch origin.
2. Switch to the port branch in a worktree where the target OS is the runtime.
3. Cherry-pick or merge from `windows`.
4. Live-test in that OS. Iterate on any breakage.
5. ONLY THEN push the port branch.

Related: [[branch-layout]], [[ubuntu-rig-workflow]], [[windows-relay-trick]].
