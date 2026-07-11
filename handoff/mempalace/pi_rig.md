---
name: pi-rig
description: "Raspberry Pi 4 (radiopi @ 192.168.4.27, user=felbs) is the third box in the rig — two working roles: SoapyRemote IQ server for live (split-decode), AND all-on-Pi DVR (record IQ now → decode offline → watch, no second box needed). Repo at ~/Software-TV-Tuner; branch pi-port-stvt. Pi has its own deep Claude memory wing (78+ files)."
metadata:
  node_type: memory
  type: project
  originSessionId: pi-relay-2026-06-08
---

The third box in the rig (discovered 2026-06-08):

- **Host:** `radiopi`, user `felbs`, last seen at `192.168.4.27` (DHCP — likely
  drifts like the Ubuntu rig per [[ubuntu-rig-workflow]]; if SSH times out,
  rescan the /24 looking for `dc:a6:32` OUI in ARP, that's the Raspberry Pi
  Foundation MAC prefix)
- **OS:** Debian trixie aarch64, kernel 6.18 RPT
- **Repo:** `~/Software-TV-Tuner/` (NOT `magic-tv-decoder` like Windows)
- **Pi-specific branch:** `pi-port-stvt` (first published 2026-06-08 from
  Windows relay at 38a82c5; advanced to 48c4000 same day with all-on-Pi DVR
  work; forks from `linux-port-stvt-v3`)

**Pi has TWO working roles now** (this was the 2026-06-08 second-push pivot —
my prior framing of "IQ server only" was incomplete):

1. **Live = split-decode (IQ server).** Pi 4 measured at ~30% real-time on the
   full ATSC chain so it can't watch live locally. Pi runs `soapyremote-server`
   on `:55132` (enabled on boot, buffers persisted via
   `/etc/sysctl.d/99-stvt-soapyremote.conf`), a fast x86 box decodes via
   `tools/stvt_decode_from_pi.sh <rf> 192.168.4.27`.

2. **DVR = all-on-Pi (record-now, decode-later).** Real-time isn't required if
   you accept offline playback. `stvt_dvr` records IQ to disk (cs16 format
   halves the bytes; `STVT_DVR_RAM=1` records to RAM for short clean
   captures), then offline-decodes and plays the result. Pi-tuned playback
   uses fast VO, no deint, HD-program, skip-warmup. Defaults: RF34, IFGR=50.
   Subcommands: `scan` (auto-ranks channels by decode quality), `verify`
   (acceptance-gate health check), `watch`. Empirical: channel choice
   dominates decode quality (RF34/RF36 clean, RF15 bad on this antenna).
   This is the answer for "watch TV on the Pi without a second machine."

Don't propose Pi local *live* decode — that's still hopeless. But all-on-Pi
DVR works; the Pi's own memory wing covers it in depth.

**Pushes from the Pi use the [[windows-relay-trick]]** — Pi has no GitHub
creds. Same recipe as Ubuntu:
```powershell
git -C Z:/src/magic-tv-decoder remote add pi-relay felbs@192.168.4.27:Software-TV-Tuner
git -C Z:/src/magic-tv-decoder fetch pi-relay pi-port-stvt
git -C Z:/src/magic-tv-decoder push origin refs/remotes/pi-relay/pi-port-stvt:refs/heads/pi-port-stvt
git -C Z:/src/magic-tv-decoder remote remove pi-relay
```
SSH key auth: my `~/.ssh/id_ed25519.pub` is in `felbs@radiopi:~/.ssh/authorized_keys`.

**Pi has its own deep Claude memory wing.** `~/.claude/projects/-home-felbs-Software-TV-Tuner/memory/` holds 78+ files from prior Pi sessions — full chain-debug history (drought analysis, fs_checker patches, equalizer tuning, etc.) plus the Pi-specific feasibility/split-decode findings. When briefing the Pi's Claude, ADD cross-box observations (things only visible from comparing setups) rather than duplicating what's already there. The Pi also has mempalace MCP installed (`~/.mempalace-home`) per `~/mempalace_pi_install.md`.

**On 2026-06-08 I added three briefings to the Pi's memory wing:**
- `cross_box_branch_layout.md` — map of all four port branches and ownership
- `fused_gain_per_setup.md` — FUSED_GAIN=1.9 doesn't transfer Ubuntu→Windows; expect same on Pi
- `pi_port_stvt_published.md` — record of the first push + relay recipe

(Note: `cross_box_branch_layout` froze pi-port-stvt at 38a82c5 / "IQ server"
description — outdated within hours by the all-on-Pi DVR work. If you write
more cross-box briefings, mark them as snapshots; don't try to keep them
synced with every push.)

**Operational gotcha — PowerShell stdin → ssh mangles text.** PowerShell
piping a heredoc/here-string to `ssh felbs@pi "cat > file"` adds a UTF-16 BOM
and replaces em-dashes (`—`) with `?`. For any text file going to the Pi
(memories, scripts, configs): write the file locally with the Write tool
first, then `scp` it. Don't pipe.

How to apply: when the user says "the Pi" / "the ARM box" / "the SDR server,"
this is the box. Pushes go via Windows. Live decode = no, all-on-Pi DVR = yes.
Briefings for the Pi's Claude live in the memory path above — they auto-load
there.

Related: [[ubuntu-rig-workflow]], [[windows-relay-trick]], [[branch-layout]],
[[no-push-linux-without-test]], [[user-role]].
