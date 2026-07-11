---
name: windows-relay-trick
description: How to push Ubuntu commits to GitHub when Ubuntu has no GitHub credentials but Windows does
metadata: 
  node_type: memory
  type: project
  originSessionId: 5e2ec3a5-5dce-46ba-9541-4f3e423c8039
---

The Ubuntu rig (felbs@<RIG_IP>) has no stored GitHub credentials — no `.git-credentials`, no `gh` CLI, no SSH-to-GitHub key. The user pushes interactively when they remember, but the credential cache doesn't survive.

**The IP drifts.** Originally `192.168.4.28`; after a reboot + port-move on 2026-05-30 it came back as `192.168.4.49`. There's no static DHCP reservation. If SSH to the last-known IP times out and `ping` shows the router replying "destination host unreachable," the rig is up but on a new lease. Find it by scanning the /24 for hosts with port 22 open — it's typically the only one on a home LAN. PowerShell scan (PS 5.1, no `-Parallel`):
```powershell
$p = New-Object System.Net.NetworkInformation.Ping; 1..254 | %{ try { if (($p.Send("192.168.4.$_",200)).Status -eq 'Success') { "192.168.4.$_" } } catch {} } | %{ $c=New-Object Net.Sockets.TcpClient; $ar=$c.BeginConnect($_,22,$null,$null); if ($ar.AsyncWaitHandle.WaitOne(300,$false) -and $c.Connected) { "$_ SSH-OPEN" }; $c.Close() }
```
Confirm with the user before SSHing the new IP (the auto-mode classifier will block silent host swaps). After confirming, `ssh-keyscan -T 5 <new-ip> >> ~/.ssh/known_hosts` so git-over-SSH works.

Windows (`Z:\src\magic-tv-decoder`) DOES have working GitHub auth via Git Credential Manager, cached under username `dzsc9c5zvv-hub` (a Codespaces-style automation account / PAT pseudo-username from previous Ubuntu-Claude sessions). Verify with:
```
printf "protocol=https\nhost=github.com\n\n" | git credential fill
```

**Relay technique** — pushes Ubuntu commits to GitHub through Windows without touching the Windows working tree (which usually has stale uncommitted modifications):

```powershell
$REPO = 'Z:/src/magic-tv-decoder'
git -C $REPO remote add felbs-linux ssh://felbs@<RIG_IP>/home/felbs/Software-TV-Tuner
git -C $REPO fetch felbs-linux linux-port-stvt-v2
git -C $REPO push origin FETCH_HEAD:refs/heads/linux-port-stvt-v2
git -C $REPO remote remove felbs-linux
```

Confirm with `git -C $REPO ls-remote origin linux-port-stvt-v2` — the local `origin/linux-port-stvt-v2` ref doesn't update because Windows's fetch refspec is `+refs/heads/main:refs/remotes/origin/main` (main only); `ls-remote` queries GitHub directly.

**Why no checkout / no stash:** Windows main has a dirty working tree (modified `gr-atscplus/CMakeLists.txt`, `gr-atscplus/lib/atsc_equalizer_long_impl.h`, `tools/tv_live.py`, plus untracked `COMHMIT_TV_LIVE_DRAFT.md` and `gr-atscplus/_build_eco.bat`). Switching to linux-port-stvt-v2 would either fail or carry those changes over. Pushing FETCH_HEAD directly bypasses the working tree entirely — Windows stays on dirty main, nothing moves.

**Why:** discovered on 2026-05-18 when the user was heading to work and refused the SSH-key-to-GitHub flow. The Ubuntu Claude proposed a similar plan (with `git checkout` + `git merge --ff-only`) but didn't account for the dirty Windows tree; this FETCH_HEAD variant is the safer adaptation.

**How to apply:** any time the user says "push the linux port" / "update GitHub from Ubuntu" / "sync Ubuntu up." Default to this relay rather than trying to set up auth on Ubuntu. Only set up auth on Ubuntu (SSH key or PAT) if the user explicitly wants direct Ubuntu→GitHub pushes for some other reason. Always do an `ls-remote` verification at the end since the local `origin/<branch>` ref will look stale.

See also [[ubuntu-rig-workflow]], [[project-topology]].
