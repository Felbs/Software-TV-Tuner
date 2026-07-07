"""e7_vote.py — E7 double-decode voting: union-merge N diverse decodes.

The last build of the Fable era. Premise: decode failure is partly
TRAJECTORY-dependent — the FPLL/equalizer adaptation path, not just the
RF, decides which GOPs survive a marginal stretch. So decode the SAME
IQ N times with different starting offsets (STVT_IQ_SKIP), harvest the
clean GOPs from each pass keyed by their video PTS (content-derived, so
identical across passes), and emit the UNION: a GOP plays if ANY pass
decoded it cleanly. Time diversity becomes frames.

v1 emits PSI + video union (the metric build); audio interleave comes
after the physics is proven. Honest metric: ffmpeg null-sink frames,
union vs best single pass.

    python e7_vote.py capture.cs16 --passes 3 [--prog N] [--keep]
"""
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from harvest_player import parse_psi, PKT   # noqa: E402

PY = sys.executable
REPLAY = Path(r"Z:\src\magic-tv-decoder\tools\tv_replay.py")

CLIFF_ENV = {
    "STVT_VITERBI": "soft", "STVT_RS": "erasure", "STVT_RS_ERASURES": "14",
    "STVT_SOVA": "1", "STVT_EQ": "long", "STVT_TEISCRUB": "0",
    "STVT_FPLL_FOLD": "1",
}


def read_pts(pkt):
    """First video PTS in a PUSI packet, or None."""
    try:
        off = 4 + (pkt[4] + 1 if (pkt[3] & 0x20) else 0)
        if pkt[off:off + 3] != b"\x00\x00\x01":
            return None
        if not (pkt[off + 7] & 0x80):          # PTS_DTS_flags
            return None
        p = pkt[off + 9:off + 14]
        return (((p[0] >> 1) & 0x7) << 30) | (p[1] << 22) | \
               ((p[2] >> 1) << 15) | (p[3] << 7) | (p[4] >> 1)
    except IndexError:
        return None


def heal_stream(base_ts, donors, out_path, prog=None):
    """v2 merge — substitution, not filtration: walk the FULL base stream
    (audio/PSI continuity preserved), and wherever a base video GOP is
    damaged, splice a clean same-PTS twin from a donor pass. Never drop:
    damaged GOPs with no clean twin stay (ffmpeg still renders partial
    frames). Returns (healed_count, damaged_total)."""
    data = Path(base_ts).read_bytes()
    pmts, pids = parse_psi(data[:8_000_000])
    if not pmts:
        Path(out_path).write_bytes(data)
        return 0, 0
    if prog is None or prog not in pmts:
        prog = max(pmts, key=lambda g: pids.get(pmts[g][1], 0))
    _, vpid, _ = pmts[prog]
    out = bytearray()
    chunk = bytearray()
    chunk_clean = True
    chunk_pts = None
    last_cc = None
    healed = damaged = 0

    def flush():
        nonlocal healed, damaged
        if not chunk:
            return
        if chunk_clean:
            out.extend(chunk)
            return
        damaged += 1
        twin = donors.get(chunk_pts) if chunk_pts is not None else None
        if twin:
            out.extend(twin)
            healed += 1
        else:
            out.extend(chunk)

    i = data.find(b"\x47")
    while i >= 0 and i + PKT <= len(data):
        p = data[i:i + PKT]
        if p[0] != 0x47:
            i = data.find(b"\x47", i + 1)
            continue
        pid = ((p[1] & 0x1F) << 8) | p[2]
        if pid != vpid:
            out += p                      # audio/PSI/other: pass through
            i += PKT
            continue
        cc = p[3] & 0x0F
        has_payload = p[3] & 0x10
        pusi = p[1] & 0x40
        tei = p[1] & 0x80
        boundary = pusi and (b"\x00\x00\x01\xb3" in p or
                             b"\x00\x00\x01\xb8" in p)
        if boundary:
            flush()
            chunk = bytearray()
            chunk_clean = True
            chunk_pts = read_pts(p)
            last_cc = None
        if tei:
            chunk_clean = False
        if has_payload and last_cc is not None \
                and cc != ((last_cc + 1) & 0x0F):
            chunk_clean = False
        if has_payload:
            last_cc = cc
        chunk += p
        i += PKT
    flush()
    Path(out_path).write_bytes(bytes(out))
    return healed, damaged


def gop_chunks(ts_path, prog=None):
    """-> (psi_cache, psi_audio_packets, {pts: clean_gop_chunk_bytes})."""
    data = Path(ts_path).read_bytes()
    pmts, pids = parse_psi(data[:8_000_000])
    if not pmts:
        return None, b"", {}
    if prog is None or prog not in pmts:
        prog = max(pmts, key=lambda g: pids.get(pmts[g][1], 0))
    pmt_pid, vpid, apids = pmts[prog]
    psi_pids = {0, pmt_pid}
    psi = bytearray()
    chunks = {}
    chunk = bytearray()
    chunk_clean = False
    chunk_pts = None
    last_cc = None
    psi_seen = 0
    i = data.find(b"\x47")
    while i >= 0 and i + PKT <= len(data):
        p = data[i:i + PKT]
        if p[0] != 0x47:
            i = data.find(b"\x47", i + 1)
            continue
        pid = ((p[1] & 0x1F) << 8) | p[2]
        tei = p[1] & 0x80
        if pid in psi_pids and not tei and psi_seen < 40:
            psi += p                            # header dose of PAT/PMT
            psi_seen += 1
        elif pid == vpid:
            cc = p[3] & 0x0F
            has_payload = p[3] & 0x10
            pusi = p[1] & 0x40
            boundary = pusi and (b"\x00\x00\x01\xb3" in p or
                                 b"\x00\x00\x01\xb8" in p)
            if boundary:
                if chunk and chunk_clean and chunk_pts is not None:
                    chunks[chunk_pts] = bytes(chunk)
                chunk = bytearray()
                chunk_clean = True
                chunk_pts = read_pts(p)
                last_cc = None
            if tei:
                chunk_clean = False
            if has_payload and last_cc is not None \
                    and cc != ((last_cc + 1) & 0x0F):
                chunk_clean = False
            if has_payload:
                last_cc = cc
            chunk += p
        i += PKT
    if chunk and chunk_clean and chunk_pts is not None:
        chunks[chunk_pts] = bytes(chunk)
    return prog, bytes(psi), chunks


def null_sink_frames(ts_path, with_errors=False):
    r = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", str(ts_path), "-f", "null", "-"],
        capture_output=True, text=True)
    fr = 0
    for tok in (r.stderr or "").split("frame="):
        try:
            fr = int(tok.strip().split()[0])
        except (ValueError, IndexError):
            pass
    if not with_errors:
        return fr
    err = sum(1 for ln in (r.stderr or "").splitlines()
              if "error" in ln.lower() or "corrupt" in ln.lower())
    return fr, err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("iq")
    ap.add_argument("--passes", type=int, default=3)
    ap.add_argument("--prog", type=int)
    ap.add_argument("--skip-step", type=int, default=40000,
                    help="IQ samples of start-offset diversity per pass")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="e7_"))
    passes = []
    for k in range(args.passes):
        out = work / f"pass{k}.ts"
        log = work / f"pass{k}.log"
        env = dict(os.environ, **CLIFF_ENV)
        env["STVT_IQ_SKIP"] = str(k * args.skip_step)
        # pass diversity axis 2: alternate DFE on odd passes
        if k % 2 == 1:
            env["STVT_EQ_DFE"] = "1"
        print(f"[e7] pass {k}: skip={env['STVT_IQ_SKIP']} "
              f"dfe={env.get('STVT_EQ_DFE', '0')}", flush=True)
        subprocess.run([PY, "-u", str(REPLAY), "--iq", args.iq,
                        "--out", str(out), "--log", str(log)],
                       env=env, check=True)
        passes.append(out)

    union = {}
    per_pass = []
    psi_best = b""
    prog = args.prog
    for k, ts in enumerate(passes):
        prog_k, psi, chunks = gop_chunks(ts, prog)
        per_pass.append(chunks)
        if len(psi) > len(psi_best):
            psi_best, prog = psi, prog_k
        for pts, blob in chunks.items():
            union.setdefault(pts, blob)
        print(f"[e7] pass {k}: {len(chunks)} clean GOPs", flush=True)

    only_counts = [len(c) for c in per_pass]
    uniq = {k: sum(1 for c in per_pass if k in c) for k in union}
    exclusive = sum(1 for v in uniq.values() if v == 1)
    print(f"[e7] union: {len(union)} clean GOPs "
          f"(best single {max(only_counts) if only_counts else 0}, "
          f"{exclusive} GOPs from exactly one pass)", flush=True)

    # v2 merge: heal the fullest pass with clean twins from the others
    frs = [null_sink_frames(t) for t in passes]
    base_i = max(range(len(passes)), key=lambda k: frs[k])
    outp = Path(args.out) if args.out else work / "e7_healed.ts"
    healed, damaged = heal_stream(passes[base_i], union, outp, prog)
    print(f"[e7] base=pass{base_i} ({frs[base_i]} frames): "
          f"{healed}/{damaged} damaged GOPs healed", flush=True)

    # honest metrics: null-sink frames + decoder error lines (glitch proxy)
    fr_u, err_u = null_sink_frames(outp, with_errors=True)
    _, err_b = null_sink_frames(passes[base_i], with_errors=True)
    win = fr_u > max(frs, default=0) or err_u < err_b
    print(f"[e7] frames: passes={frs} healed={fr_u}; "
          f"decode-errors: base={err_b} healed={err_u} -> "
          f"{'WIN' if win else 'no gain'}", flush=True)
    print(f"[e7] healed file: {outp}")
    if not args.keep:
        for t in passes:
            t.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
