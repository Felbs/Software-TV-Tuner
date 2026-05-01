#!/usr/bin/env python3
"""Synthetic ATSC-IQ benchmark harness.

Generates an ATSC 8-VSB IQ stream from random transport-stream input,
adds AWGN at a specified SNR (and optionally a multipath channel),
runs each combo from combos.yaml, and scores them against ground truth.

Usage:
    python benchmark_synth.py [--snr 14] [--channel awgn] [--combo full_stack]
                              [--seconds 8] [--out results/<date>.md]

If --combo is omitted, every combo in combos.yaml is run (full sweep).
If --snr is omitted, every snr_db in combos.yaml is swept.

Designed to run in a clean Linux environment with gnuradio + the
gr-atscplus OOT module already built and installed.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).parent
COMBOS = yaml.safe_load((ROOT / "combos.yaml").read_text())

# ATSC parameters
ATSC_SYM_RATE = 10_762_237.0   # Hz, exact
ATSC_SEG_LEN = 832             # symbols per data segment (incl. 4-symbol seg-sync)
ATSC_FIELD_SEGS = 313          # segments per field
ATSC_LEVELS = np.array([-7, -5, -3, -1, 1, 3, 5, 7], dtype=np.float32)


# ----------------------------------------------------------------------
# Synthetic ATSC IQ generation
# ----------------------------------------------------------------------

def _make_ts_packets(n_pkts: int, rng: np.random.Generator) -> bytes:
    KEEP_PIDS = [0x0030, 0x0031, 0x0032, 0x0033, 0x0034, 0x0035]
    buf = bytearray(n_pkts * 188)
    for i in range(n_pkts):
        base = i * 188
        pid = KEEP_PIDS[i % len(KEEP_PIDS)]
        buf[base] = 0x47
        buf[base + 1] = (pid >> 8) & 0x1F
        buf[base + 2] = pid & 0xFF
        buf[base + 3] = 0x10 | ((i >> 3) & 0x0F)
        payload = rng.integers(0, 256, 184, dtype=np.uint8).tobytes()
        buf[base + 4:base + 188] = payload
    return bytes(buf)


def _run_atsc_encoder(ts_data: bytes) -> bytes:
    import gnuradio.dtv as dtv
    from gnuradio import gr, blocks
    class EncTB(gr.top_block):
        def __init__(self, data):
            gr.top_block.__init__(self)
            padded = bytearray(data)
            rem = len(padded) % 188
            if rem:
                padded.extend(b'\x00' * (188 - rem))
            src = blocks.vector_source_b(list(padded), repeat=False)
            self.sink = blocks.vector_sink_b(vlen=1024)
            self.connect(src,
                         dtv.atsc_pad(), dtv.atsc_randomizer(),
                         dtv.atsc_rs_encoder(), dtv.atsc_interleaver(),
                         dtv.atsc_trellis_encoder(), dtv.atsc_field_sync_mux(),
                         self.sink)
    tb = EncTB(ts_data)
    tb.run()
    return bytes(bytearray(tb.sink.data()))


def _encoder_symbols(ts_data: bytes) -> np.ndarray:
    raw = _run_atsc_encoder(ts_data)
    raw_arr = np.frombuffer(raw, dtype=np.uint8)
    n_segs = len(raw_arr) // 1024
    indices = np.lib.stride_tricks.as_strided(
        raw_arr[4:], shape=(n_segs, ATSC_SEG_LEN),
        strides=(1024, 1)).astype(np.float32).copy()
    return (2.0 * indices - 7.0).ravel()


def srrc_taps(beta: float = 0.1152, sps: int = 2, span: int = 16) -> np.ndarray:
    """Square-root raised cosine filter coefficients (truncated)."""
    n = span * sps
    t = np.arange(-n, n + 1) / sps
    pi = np.pi
    out = np.zeros_like(t)
    for i, ti in enumerate(t):
        if ti == 0:
            out[i] = 1.0 - beta + 4 * beta / pi
        elif abs(abs(ti) - 1 / (4 * beta)) < 1e-9:
            out[i] = (beta / np.sqrt(2)) * (
                (1 + 2 / pi) * np.sin(pi / (4 * beta))
                + (1 - 2 / pi) * np.cos(pi / (4 * beta)))
        else:
            num = (np.sin(pi * ti * (1 - beta))
                   + 4 * beta * ti * np.cos(pi * ti * (1 + beta)))
            den = pi * ti * (1 - (4 * beta * ti) ** 2)
            out[i] = num / den
    out /= np.sqrt(np.sum(out ** 2))
    return out.astype(np.float32)


def synthesize_atsc_iq(seconds: float, snr_db: float, channel: dict,
                       seed: int, sample_rate: int = 8_000_000,
                       sps_at_sym: int = 2) -> np.ndarray:
    """Return interleaved int16 IQ at sample_rate that emulates ATSC RF.

    Pipeline:
      GR ATSC encoder chain (field syncs, RS, trellis) from random TS packets
      → upsample to ATSC_SYM_RATE * sps_at_sym
      → SRRC pulse shape
      → vestigial sideband (Hilbert + pilot at -2.691 MHz)
      → multipath channel (paths from combos.yaml)
      → AWGN at given SNR
      → resample via exact rational 572/1539
      → quantize to int16 interleaved
    """
    rng = np.random.default_rng(seed)
    segs_needed = int(np.ceil(seconds * ATSC_SYM_RATE / ATSC_SEG_LEN)) + 700
    n_pkts = segs_needed + 100
    ts_data = _make_ts_packets(n_pkts, rng)
    symbols = _encoder_symbols(ts_data)
    n_segs = len(symbols) // ATSC_SEG_LEN
    symbols = symbols[:n_segs * ATSC_SEG_LEN]

    # Upsample by sps_at_sym
    up = np.zeros(symbols.size * sps_at_sym, dtype=np.float32)
    up[::sps_at_sym] = symbols

    # SRRC shaping
    h = srrc_taps(beta=0.1152, sps=sps_at_sym, span=16)
    shaped = np.convolve(up, h, mode='same')

    # Internal rate after shaping
    internal_rate = ATSC_SYM_RATE * sps_at_sym

    # Apply multipath
    paths = channel.get("paths", []) if channel else []
    if paths:
        max_delay = max(p["delay_us"] for p in paths) * 1e-6
        max_delay_samples = int(np.ceil(max_delay * internal_rate))
        h_chan = np.zeros(max_delay_samples + 1, dtype=np.complex64)
        h_chan[0] = 1.0
        for p in paths:
            tap_idx = int(round(p["delay_us"] * 1e-6 * internal_rate))
            h_chan[tap_idx] += 10 ** (p["gain_db"] / 20.0)
        shaped = np.convolve(shaped, h_chan.real, mode='same').astype(np.float32)

    # ATSC spec: pilot = 0.307 × RMS data ≈ 1.407; 0.5 is 9 dB too weak
    shaped = shaped + 1.4

    # Form analytic IQ via Hilbert (positive sideband only)
    from scipy.signal import hilbert
    analytic = hilbert(shaped).astype(np.complex64)

    # Shift down by -2.691 MHz so pilot lands where atsc_fpll expects it
    n = analytic.size
    t_idx = np.arange(n)
    f_pilot = -2.691e6
    analytic = analytic * np.exp(1j * 2 * np.pi * f_pilot * t_idx / internal_rate)

    # Add AWGN at requested SNR
    sig_p = float(np.mean(np.abs(analytic) ** 2))
    snr_lin = 10 ** (snr_db / 10.0)
    noise_p = sig_p / snr_lin
    noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * np.sqrt(noise_p / 2)
    analytic = analytic + noise.astype(np.complex64)

    # Resample to target sample_rate
    # Exact: (ATSC_SYM_RATE×2) × 572/1539 ≈ 8000000 (sub-ppm error, tractable filter)
    if sample_rate != int(internal_rate):
        from scipy.signal import resample_poly
        analytic = resample_poly(analytic, 572, 1539).astype(np.complex64)

    # Convert to int16 interleaved
    peak = max(np.max(np.abs(analytic.real)), np.max(np.abs(analytic.imag)), 1e-9)
    scale = 32767 * 0.5 / peak
    out = np.empty(analytic.size * 2, dtype=np.int16)
    out[0::2] = np.clip(analytic.real * scale, -32768, 32767).astype(np.int16)
    out[1::2] = np.clip(analytic.imag * scale, -32768, 32767).astype(np.int16)
    return out


# ----------------------------------------------------------------------
# Decode + score
# ----------------------------------------------------------------------

def scrub_tei(ts_data: bytes) -> tuple[bytes, dict]:
    """Rewrite TEI=1 packets to NULL packets, return (cleaned, metrics)."""
    KEEP = {0x0000, 0x1FFB, 0x1FFF}
    for p in range(0x30, 0x90, 0x10):
        for s in range(6):
            KEEP.add(p | s)
    data = bytearray(ts_data)
    n = len(data) // 188
    clean = 0
    for i in range(n):
        b = i * 188
        if data[b] != 0x47:
            data[b] = 0x47
            data[b + 1] = 0x1F
            data[b + 2] = 0xFF
            data[b + 3] = 0x10
            continue
        tei = (data[b + 1] >> 7) & 1
        pid = ((data[b + 1] & 0x1F) << 8) | data[b + 2]
        if tei or pid not in KEEP:
            data[b + 1] = 0x1F
            data[b + 2] = 0xFF
        else:
            clean += 1
    return bytes(data), dict(total=n, clean=clean,
                              clean_frac=clean / max(n, 1))


def parse_fs_checker_stats(stderr: str) -> dict:
    m = re.search(r"\[fs_checker_inst FINAL\] segments=(\d+) "
                  r"pn511_hits=(\d+) field1=(\d+) field2=(\d+) uncertain=(\d+)", stderr)
    if not m:
        return {}
    return dict(segments=int(m.group(1)),
                pn511_hits=int(m.group(2)),
                field1=int(m.group(3)),
                field2=int(m.group(4)),
                uncertain=int(m.group(5)))


def run_one(combo_name: str, iq_path: Path, ts_path: Path) -> dict:
    """Run run_combo.py + collect metrics."""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "run_combo.py"),
         str(iq_path), str(ts_path), combo_name],
        capture_output=True, text=True, timeout=600)
    fs_stats = parse_fs_checker_stats(proc.stderr)

    if not ts_path.exists() or ts_path.stat().st_size == 0:
        return dict(combo=combo_name, decode_ok=False, **fs_stats)

    cleaned, scrub = scrub_tei(ts_path.read_bytes())
    ts_path.write_bytes(cleaned)
    return dict(combo=combo_name, decode_ok=True,
                rs_clean_frac=scrub["clean_frac"],
                rs_clean_count=scrub["clean"],
                total_packets=scrub["total"],
                **fs_stats)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snr", type=float, default=None,
                    help="Single SNR (dB). Default: sweep COMBOS['snr_db'].")
    ap.add_argument("--channel", default=None,
                    help="Single channel name. Default: sweep COMBOS['channels'].")
    ap.add_argument("--combo", default=None,
                    help="Single combo name. Default: sweep all combos.")
    ap.add_argument("--seconds", type=float, default=8.0,
                    help="IQ duration to synthesize.")
    ap.add_argument("--out", default=None,
                    help="Output Markdown path. Default: results/YYYY-MM-DD.md")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    snr_list = [args.snr] if args.snr is not None else COMBOS["snr_db"]
    if args.channel:
        chan_list = [c for c in COMBOS["channels"] if c["name"] == args.channel]
        if not chan_list:
            sys.exit(f"unknown channel {args.channel}")
    else:
        chan_list = COMBOS["channels"]
    combo_list = ([c for c in COMBOS["combos"] if c["name"] == args.combo]
                  if args.combo else COMBOS["combos"])
    if args.combo and not combo_list:
        sys.exit(f"unknown combo {args.combo}")

    rows = []
    out_md = (Path(args.out) if args.out
              else ROOT / "results" / f"{datetime.date.today().isoformat()}.md")
    out_md.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        for chan in chan_list:
            for snr in snr_list:
                iq_path = tmpd / f"iq_{chan['name']}_snr{snr}.cs16"
                if not iq_path.exists():
                    print(f"[gen] channel={chan['name']} snr={snr} ...", flush=True)
                    iq = synthesize_atsc_iq(args.seconds, snr, chan, args.seed)
                    iq.tofile(iq_path)

                for combo in combo_list:
                    name = combo["name"]
                    ts_path = tmpd / f"out_{chan['name']}_{snr}_{name}.ts"
                    print(f"[run] {chan['name']} snr={snr} combo={name}", flush=True)
                    res = run_one(name, iq_path, ts_path)
                    res.update(dict(channel=chan["name"], snr_db=snr))
                    rows.append(res)
                    ts_path.unlink(missing_ok=True)

    # Write Markdown report
    lines = [f"# ATSC combo sweep — {datetime.datetime.utcnow().isoformat(timespec='seconds')}Z\n",
             f"**Channels:** {[c['name'] for c in chan_list]}",
             f"**SNR (dB):** {snr_list}",
             f"**Combos:** {[c['name'] for c in combo_list]}\n"]
    lines.append("| channel | snr_db | combo | RS-clean | PN511 hits | field syncs |")
    lines.append("|---------|--------|-------|----------|------------|-------------|")
    for r in rows:
        rsc = f"{100 * r.get('rs_clean_frac', 0):.1f}%" if r.get("decode_ok") else "fail"
        hits = r.get("pn511_hits", "-")
        fs = (r.get("field1", 0) or 0) + (r.get("field2", 0) or 0)
        lines.append(f"| {r['channel']} | {r['snr_db']} | {r['combo']} | "
                     f"{rsc} | {hits} | {fs} |")
    out_md.write_text("\n".join(lines) + "\n")
    print(f"[done] {len(rows)} rows → {out_md}")
    json_path = out_md.with_suffix(".json")
    json_path.write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
