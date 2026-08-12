"""e4_vhf_pn511.py — VHF field-sync probe, CORRECTED knob (2026-07-30).

The first E4 attempt swept ATSCPLUS_FS_TOL_LOW (280 -> 120 -> 60). That knob is
NOT a correlation threshold: it is the segment-GAP spacing validator, and it is
only consulted when d_fs_locked is ALREADY true. On a channel that never locks
even once it is a no-op, so those arms were placebos.

The real correlation gate is ATSCPLUS_PN511_LIMIT: bit errors allowed out of the
511-bit PN sequence, default 50, env range 10..220. 220/511 = 43% BER ~ a coin
flip, so the max arm is a definitive test: if RF9 yields no accepted field sync
at limit 220 with spacing validation OFF, the field sync is not recoverable from
this capture and the answer is longer integration, not a looser threshold.

Capture verified before the sweep: pilot at -2.690 MHz (exactly ATSC's
+309.44 kHz above the 186 MHz lower edge at a 189 MHz center), rms ~1071,
in-band/out-of-band +12 dB. Signal present and correctly centered.
"""
import os
import re
import subprocess
from pathlib import Path

PY = os.path.join(os.environ.get("USERPROFILE", ""), "radioconda", "python.exe")
REPO = Path(r"Z:\src\magic-tv-decoder")
N3 = REPO / "lab" / "night3"
IQ = N3 / "rf9_probe.cs16"

ARMS = [
    ("base",     {}),
    ("pn100",    {"ATSCPLUS_PN511_LIMIT": "100"}),
    ("pn160",    {"ATSCPLUS_PN511_LIMIT": "160"}),
    ("pn220max", {"ATSCPLUS_PN511_LIMIT": "220", "ATSCPLUS_PN63_LIMIT": "30"}),
]


def frames(ts: Path) -> int:
    """Frame count via ffmpeg null sink — the only honest metric (ffprobe lies
    on multi-program TS)."""
    if not ts.exists() or ts.stat().st_size < 100_000:
        return 0
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
                        "-i", str(ts), "-map", "0:v", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=900)
    m = re.findall(r"frame=\s*(\d+)", r.stderr)
    return int(m[-1]) if m else 0


def main():
    assert IQ.exists(), IQ
    print(f"=== E4 corrected: PN511 correlation sweep on {IQ.name} "
          f"({IQ.stat().st_size/1e9:.2f} GB) ===", flush=True)
    for name, extra in ARMS:
        env = dict(os.environ)
        # Spacing validator OFF in every arm: we are testing the CORRELATION
        # gate, and a spacing reject would confound it.
        env["ATSCPLUS_FS_VALIDATE"] = "0"
        env["ATSCPLUS_FS_TELEM"] = "1"
        env.update(extra)
        ts = N3 / f"rf9_{name}.ts"
        log = N3 / f"rf9_{name}.log"
        subprocess.run([PY, str(REPO / "tools" / "tv_replay.py"),
                        "--iq", str(IQ), "--out", str(ts), "--log", str(log)],
                       cwd=str(REPO), env=env, timeout=3600)
        txt = log.read_text(errors="ignore") if log.exists() else ""
        acc = re.findall(r"\[fs_check\] accepted=(\d+)", txt)
        seq = len(re.findall(r"aligned=|SEQ|sequence header", txt))
        lim = extra.get("ATSCPLUS_PN511_LIMIT", "50")
        print(f"E4 {name:9s} pn511_limit={lim:>3} accepted={acc[-1] if acc else 'n/a'} "
              f"ts={ts.stat().st_size/1e6 if ts.exists() else 0:.0f}MB "
              f"frames={frames(ts)} telem_lines={txt.count('[fs_telem')}",
              flush=True)


if __name__ == "__main__":
    main()
