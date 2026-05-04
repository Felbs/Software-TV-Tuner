"""ATSC CEA-608 closed-caption decoder — pure Python, stdlib only.

Reads an MPEG-TS file (live.ts by default) and prints the live
closed-caption text to stdout. Used by tv_tuner.py's --cc flag when
the external `ccextractor` tool isn't installed. ccextractor handles
both CEA-608 and CEA-708 and is preferred when available; this
fallback handles the much-more-common CEA-608 line-21 captions on
field 1 / channel 1 (English primary), which covers the vast majority
of broadcast TV.

Format reference: ATSC A/53 Part 4, CEA-608-E.

Pipeline:
  1. Read TS packets (188 bytes each) from the input file. Tail-follow
     the file as new bytes arrive (live.ts is being written by tv_live).
  2. Reassemble video-PID PES payloads. We don't bother parsing PMT —
     we just scan every PID's payload for the mpeg2video user-data
     start code (0x000001B2). Cheap and correct enough.
  3. After 0x000001B2, check for the ATSC identifier 'GA94' followed
     by user_data_type_code 0x03 (cc_data).
  4. Parse cc_count, then cc_count entries of (cc_valid+cc_type, b1, b2).
     Filter to cc_type == 0 (NTSC field 1). Strip parity bits.
  5. Run the byte pairs through a CEA-608 state machine: print printable
     characters, treat control codes (CR, EOC, EDM) as line breaks.

This is a deliberately simplified decoder — no positioning, no colors,
no italics, no pop-on/paint-on/roll-up distinction. Just a stream of
plain text in the order the broadcaster sent it.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# CEA-608 has minor differences from ASCII — these byte values map to
# accented Spanish characters in the standard. Mapping pulled from
# CEA-608-E table 5.
CEA608_BASIC_MAP = {
    0x2A: "á",
    0x5C: "é",
    0x5E: "í",
    0x5F: "ó",
    0x60: "ú",
    0x7B: "ç",
    0x7C: "÷",
    0x7D: "Ñ",
    0x7E: "ñ",
    0x7F: "█",
}

# Special characters introduced by 0x11/0x19 + 0x30..0x3F.
CEA608_SPECIAL_CHARS = "®°½¿™¢£♪à èâêîôû"


def cc_decode_byte(b: int) -> str:
    """Map a single CEA-608 character byte to a Unicode string.
    Returns "" for non-printable bytes."""
    b &= 0x7F  # strip parity
    if b in CEA608_BASIC_MAP:
        return CEA608_BASIC_MAP[b]
    if 0x20 <= b <= 0x7F:
        return chr(b)
    return ""


class CC608Decoder:
    """Stateful CEA-608 byte-pair decoder.  Emits text fragments and
    line breaks; the caller writes them to stdout."""

    def __init__(self):
        self.last_pair: tuple[int, int] | None = None
        self.in_special = False  # awaiting second byte of an extended char

    def feed_pair(self, b1: int, b2: int) -> str:
        """Feed one CEA-608 byte pair. Returns any text/control output."""
        b1 &= 0x7F
        b2 &= 0x7F
        # Skip null pairs
        if b1 == 0 and b2 == 0:
            return ""
        # Filter repeats — line-21 captions are sent twice for redundancy
        # (both fields), but we only care about field 1, so duplicate
        # control-code suppression here is mostly a guard against the
        # dual-write of preamble address codes.
        if (b1, b2) == self.last_pair and 0x10 <= b1 <= 0x1F:
            self.last_pair = None
            return ""
        self.last_pair = (b1, b2)

        # Control codes: 0x10..0x1F (control or preamble first byte).
        if 0x10 <= b1 <= 0x17:
            # Channel-select control codes are paired with second byte
            # in 0x20..0x7F (preamble address) or 0x20..0x2F (mid-row).
            return self._handle_control(b1, b2)
        if 0x18 <= b1 <= 0x1F:
            # Channel-2 control codes — skip; we only show channel 1.
            return ""
        # Two printable characters back-to-back.
        return cc_decode_byte(b1) + cc_decode_byte(b2)

    def _handle_control(self, b1: int, b2: int) -> str:
        # 0x14, 0x15 are channel-1 control prefix bytes. 0x16, 0x17 are
        # extended/Spanish chars. 0x11 is special chars.
        # Most-common channel-1 commands (0x14 + xx):
        #   0x14 0x2C = EDM (erase displayed memory)
        #   0x14 0x2D = CR  (carriage return — roll-up scroll)
        #   0x14 0x2E = ENM (erase non-displayed memory)
        #   0x14 0x2F = EOC (end of caption — swap)
        #   0x14 0x25..0x27 = RU2/3/4 (roll-up start)
        #   0x14 0x20 = RCL (resume caption loading — pop-on)
        if b1 in (0x14, 0x15):
            if b2 in (0x2C, 0x2D, 0x2F):  # EDM / CR / EOC → newline
                return "\n"
            return ""
        # Extended Spanish/French characters: 0x12 0x20..0x3F or
        # 0x13 0x20..0x3F.  Ignore for the basic English decoder.
        if b1 in (0x12, 0x13):
            return ""
        # Special characters: 0x11 0x30..0x3F → one of CEA608_SPECIAL_CHARS.
        if b1 == 0x11 and 0x30 <= b2 <= 0x3F:
            idx = b2 - 0x30
            if 0 <= idx < len(CEA608_SPECIAL_CHARS):
                return CEA608_SPECIAL_CHARS[idx]
            return ""
        # Mid-row codes (color/italic/underline) and preamble address
        # codes — ignored for plain-text output.
        return ""


def find_cc_data_in_userdata(blob: bytes, decoder: CC608Decoder,
                               write) -> None:
    """Scan a chunk of bytes for ATSC user-data sections that carry
    cc_data, decode the byte pairs through `decoder`, and call `write`
    with each emitted text fragment."""
    pos = 0
    n = len(blob)
    while pos < n - 8:
        # Find the next mpeg2video user_data_start_code (0x00 0x00 0x01 0xB2).
        idx = blob.find(b"\x00\x00\x01\xB2", pos)
        if idx < 0:
            return
        i = idx + 4
        if i + 5 > n:
            return
        # ATSC ATSC_user_data: identifier "GA94" (0x47 0x41 0x39 0x34)
        if blob[i:i + 4] != b"GA94":
            pos = idx + 4
            continue
        i += 4
        # user_data_type_code: 0x03 = cc_data
        ud_type = blob[i]
        i += 1
        if ud_type != 0x03:
            pos = idx + 4
            continue
        # cc_data header: 1 byte: process_em_data_flag(1) +
        #   process_cc_data_flag(1) + additional_data_flag(1) +
        #   cc_count(5)
        if i >= n:
            return
        hdr = blob[i]
        i += 1
        cc_count = hdr & 0x1F
        # 1 byte em_data (always 0xFF in ATSC)
        i += 1
        # cc_count × 3 bytes: marker_bits(5)+cc_valid(1)+cc_type(2),
        #                     cc_data_1, cc_data_2
        if i + cc_count * 3 > n:
            return
        for _ in range(cc_count):
            type_byte = blob[i]
            cc_data_1 = blob[i + 1]
            cc_data_2 = blob[i + 2]
            i += 3
            cc_valid = (type_byte >> 2) & 0x01
            cc_type = type_byte & 0x03
            # cc_type 0 = NTSC field 1, 1 = NTSC field 2,
            # 2/3 = DTVCC (CEA-708 fragments — ignored here).
            if cc_valid and cc_type == 0:
                out = decoder.feed_pair(cc_data_1, cc_data_2)
                if out:
                    write(out)
        pos = i


def tail_follow(path: Path, chunk_size: int = 64 * 1024,
                 startup_timeout: float = 30.0):
    """Yield byte chunks from `path` as the file grows. Waits at startup
    for the file to appear and to have at least chunk_size bytes."""
    deadline = time.time() + startup_timeout
    while not path.exists() and time.time() < deadline:
        time.sleep(0.5)
    if not path.exists():
        raise FileNotFoundError(f"{path} did not appear within "
                                  f"{startup_timeout} s")
    # Start near the end of the file so we get LIVE captions, not the
    # backlog from a previous tune. ~256 KB back covers ~100 ms of TS
    # which guarantees we land on a packet boundary.
    f = open(path, "rb")
    try:
        size = path.stat().st_size
        f.seek(max(0, size - 256 * 1024))
        # Align to next TS sync byte.
        leftover = b""
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                time.sleep(0.5)
                continue
            data = leftover + chunk
            # Re-align to a TS sync if we just started.
            if leftover == b"":
                sync = data.find(b"\x47")
                if sync > 0:
                    data = data[sync:]
            yield data
            # Hold last 1 KB as overlap so user_data start codes that
            # straddle chunk boundaries still get parsed cleanly.
            leftover = data[-1024:]
    finally:
        f.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ts_path", nargs="?",
                     help="Path to live MPEG-TS (default: live.ts in the "
                          "tv_live data dir)")
    args = ap.parse_args()

    if args.ts_path:
        path = Path(args.ts_path)
    else:
        # Default to the tv_tuner data dir's live.ts.
        path = Path(__file__).resolve().parent.parent / "data" / "tv_live" / "live.ts"
        if not path.exists():
            # Fallback: hunt nearby.
            here = Path(__file__).resolve().parent
            for cand in [here / "data" / "tv_live" / "live.ts",
                         here.parent / "SDR_Agent_v2" / "data" / "tv_live" / "live.ts"]:
                if cand.exists():
                    path = cand
                    break

    print(f"[atsc_cc] reading captions from {path}", file=sys.stderr)
    print("[atsc_cc] CEA-608 field 1 / channel 1 (English) only.",
          file=sys.stderr)
    print("[atsc_cc] If captions don't appear, the broadcaster may not "
          "be transmitting them.\n", file=sys.stderr)

    decoder = CC608Decoder()
    line_buf: list[str] = []

    def write_text(s: str):
        # Roll-up captions stream characters then send a CR; pop-on/
        # paint-on send EOC. Both surface as "\n" from the decoder.
        # Buffer characters into a line, flush on newline.
        if s == "\n":
            if line_buf:
                print("".join(line_buf).strip(), flush=True)
                line_buf.clear()
        else:
            line_buf.append(s)

    try:
        for chunk in tail_follow(path):
            find_cc_data_in_userdata(chunk, decoder, write_text)
    except KeyboardInterrupt:
        pass
    except FileNotFoundError as e:
        print(f"[atsc_cc] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
