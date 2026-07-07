"""harvest_player.py — universal force-play: emit only TRUE video.

The play-path law discovered 2026-07-07: below ~16 dB, raw streams are
countable-but-not-assemblable — players freeze, cone, or starve. But
loss BREATHES: clean seconds hide between torn ones. This tool parses
the transport stream GOP by GOP and passes ONLY chunks that survived
intact (no TEI, no continuity gaps on the video PID), plus audio
(which survives 2-3 dB deeper — the screen may pause; the sound
persists). The player downstream receives 100%-true, always-valid TS:
motion at reduced rhythm instead of frozen garbage.

Input material should be UNSCRUBBED (STVT_TEISCRUB=0): the scrub A/B
showed nulling costs half the recoverable stream; WE are the filter now.

    python harvest_player.py capture.ts            # offline: harvest + stats
    python harvest_player.py capture.ts --play     # harvest then mpv
    python harvest_player.py live.ts --follow      # live tail harvest + mpv
"""
import argparse
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

MPV = r"C:\Program Files\MPV Player\mpv.exe"
PKT = 188


def parse_psi(data):
    """PAT+PMTs -> {prog: (pmt_pid, video_pid, [audio_pids])}."""
    pids = Counter()
    pat = {}
    pmts = {}
    i = data.find(b"\x47")
    while i >= 0 and i + PKT <= len(data):
        p = data[i:i + PKT]
        if p[0] != 0x47:
            i = data.find(b"\x47", i + 1)
            continue
        pid = ((p[1] & 0x1F) << 8) | p[2]
        pids[pid] += 1
        pusi = p[1] & 0x40
        if pusi:
            try:
                off = 4 + (p[4] + 1 if (p[3] & 0x20) else 0)
                off += p[off] + 1
                if pid == 0:
                    slen = ((p[off + 1] & 0x0F) << 8) | p[off + 2]
                    for q in range(off + 8, off + 3 + slen - 4, 4):
                        prog = (p[q] << 8) | p[q + 1]
                        if prog:
                            pat[prog] = ((p[q + 2] & 0x1F) << 8) | p[q + 3]
                elif pid in pat.values() and pid not in [v[0] for v in pmts.values() if v]:
                    slen = ((p[off + 1] & 0x0F) << 8) | p[off + 2]
                    prog = (p[off + 3] << 8) | p[off + 4]
                    il = ((p[off + 10] & 0x0F) << 8) | p[off + 11]
                    q = off + 12 + il
                    vpid, apids = None, []
                    while q < off + 3 + slen - 4 and q + 5 <= len(p):
                        st = p[q]
                        spid = ((p[q + 1] & 0x1F) << 8) | p[q + 2]
                        el = ((p[q + 3] & 0x0F) << 8) | p[q + 4]
                        if st in (0x01, 0x02):
                            vpid = spid
                        elif st in (0x81, 0x03, 0x04, 0x0F):
                            apids.append(spid)
                        q += 5 + el
                    if vpid:
                        pmts[prog] = (pid, vpid, apids)
            except IndexError:
                pass
        i += PKT
    return pmts, pids


def harvest(path, out_path, prog=None, verbose=True):
    data = Path(path).read_bytes()
    pmts, pids = parse_psi(data[:6_000_000] if len(data) > 6_000_000 else data)
    if not pmts:
        print("no PMTs found — stream too damaged for PSI; cannot harvest")
        return 0, 0
    if prog is None or prog not in pmts:
        # fattest video pid wins
        prog = max(pmts, key=lambda g: pids.get(pmts[g][1], 0))
    pmt_pid, vpid, apids = pmts[prog]
    keep_always = {0, pmt_pid, *apids}      # PSI + audio pass-through

    out = bytearray()
    chunk = bytearray()          # current GOP-chunk (video pid packets)
    chunk_clean = True
    chunks_pass = chunks_drop = 0
    last_cc = None
    i = data.find(b"\x47")
    while i >= 0 and i + PKT <= len(data):
        p = data[i:i + PKT]
        if p[0] != 0x47:
            i = data.find(b"\x47", i + 1)
            continue
        pid = ((p[1] & 0x1F) << 8) | p[2]
        tei = p[1] & 0x80
        if pid in keep_always:
            if not tei:
                out += p
        elif pid == vpid:
            cc = p[3] & 0x0F
            has_payload = p[3] & 0x10
            pusi = p[1] & 0x40
            # GOP boundary: a NEW PES (payload_unit_start) whose payload
            # carries a sequence/GOP start code — chunks must begin where
            # a decoder can enter, not wherever a code straddles packets
            boundary = pusi and (b"\x00\x00\x01\xb3" in p or
                                 b"\x00\x00\x01\xb8" in p)
            if boundary:
                if chunk and chunk_clean:
                    out += chunk
                    chunks_pass += 1
                elif chunk:
                    chunks_drop += 1
                chunk = bytearray()
                chunk_clean = True
                last_cc = None
            if tei:
                chunk_clean = False
            if has_payload and last_cc is not None \
                    and cc != ((last_cc + 1) & 0x0F):
                chunk_clean = False       # continuity gap = torn chunk
            if has_payload:
                last_cc = cc
            chunk += p
        i += PKT
    if chunk and chunk_clean:
        out += chunk
        chunks_pass += 1
    elif chunk:
        chunks_drop += 1
    Path(out_path).write_bytes(bytes(out))
    if verbose:
        tot = chunks_pass + chunks_drop
        pct = 100 * chunks_pass / tot if tot else 0
        print(f"prog {prog} vpid {vpid}: {chunks_pass}/{tot} GOP-chunks "
              f"harvested ({pct:.0f}%), {len(out)//1024} KB true stream "
              f"-> {out_path}")
    return chunks_pass, chunks_drop


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ts")
    ap.add_argument("--prog", type=int)
    ap.add_argument("--play", action="store_true")
    ap.add_argument("--follow", action="store_true",
                    help="live: re-harvest the growing file every 5 s")
    args = ap.parse_args()
    outp = str(Path(args.ts).with_suffix(".harvested.ts"))

    if args.follow:
        mpv = None
        print("follow mode: harvesting every 5 s (patience player)")
        while True:
            try:
                harvest(args.ts, outp, args.prog, verbose=False)
            except OSError:
                pass
            if mpv is None and Path(outp).exists() \
                    and Path(outp).stat().st_size > 2_000_000:
                mpv = subprocess.Popen(
                    [MPV, outp, "--force-window=yes", "--keep-open=yes",
                     "--force-seekable=yes",
                     "--title=Harvest Player (true frames only)"])
                print("player launched", flush=True)
            time.sleep(5)
    else:
        harvest(args.ts, outp, args.prog)
        if args.play:
            subprocess.run([MPV, outp, "--force-window=yes",
                            "--keep-open=yes"])


if __name__ == "__main__":
    main()
