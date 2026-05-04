"""ATSC CEA-608 closed-caption decoder — pure Python, stdlib only.

Reads an MPEG-TS file (live.ts by default) and prints the live closed-
caption text to stdout. Used by tv_tuner.py's --cc flag when the
external `ccextractor` tool isn't installed. ccextractor handles both
CEA-608 and CEA-708 and is preferred when available; this fallback
handles CEA-608 line-21 captions on field 1 / channel 1 (English
primary), which covers nearly all broadcast TV.

Why this exists & what 1980s line-21 captions taught us
-------------------------------------------------------
Closed-captioning rolled out on US TV on March 16, 1980 (PBS WGBH).
The encoding had to survive:
  - cheap consumer decoder hardware (a single LSI chip),
  - noisy analog VBI on line 21 of NTSC,
  - lost fields from multipath, snow, and tape duplication.

The CEA-608-E design solves those constraints with three tricks that
this decoder must respect — getting any of them wrong produces the
"jumbled stream of letters" symptom we had:

  1. Odd parity per byte. Each 7-bit char has an 8th parity bit; the
     decoder strips it (`b & 0x7F`) before interpretation. We treat
     parity as advisory and don't reject pairs with bad parity, since
     digital ATSC streams already have stronger FEC upstream.

  2. Doubled control codes. Every CONTROL pair (byte 1 in 0x10–0x1F)
     is transmitted TWICE on consecutive fields. The receiver must
     suppress the duplicate. (Parity catches single-bit errors; doubling
     catches whole-field dropouts. The cheap hardware couldn't do both
     forward error correction and parity, so they doubled instead.)

  3. Channel multiplexing in field 1. CC1 (primary, e.g. English) and
     CC2 (secondary, e.g. Spanish) share the same byte stream. The
     channel of every CONTROL code is encoded in bit 3 of byte 1:
         0x10–0x17  →  CC1
         0x18–0x1F  →  CC2
     Printable bytes (0x20–0x7F) inherit the channel of the most
     recent control code. THIS IS THE CRITICAL DEMUX RULE — without
     it, CC2 text leaks into the CC1 stream as scrambled gibberish.

Three caption display modes (every consumer TV implements all three):

    pop-on  (RCL  0x14 0x20): broadcaster pre-builds an entire
        caption in "non-displayed memory"; EOC (0x14 0x2F) atomically
        swaps it onto the screen. Used for movies, dramas, sitcoms.

    roll-up (RU2  0x14 0x25,
             RU3  0x14 0x26,
             RU4  0x14 0x27): each new line writes to the bottom row;
        CR (0x14 0x2D) scrolls the previous rows up. Used for live
        news, sports, weather — anything where captions stream in
        real time.

    paint-on (RDC 0x14 0x29): characters appear on screen as received,
        no buffering. Rare; usually a transition between modes.

For plain-text stdout output we don't need positioning, color, or
italics — but we MUST honor the mode boundaries (EOC for pop-on, CR
for roll-up) or pop-on captions print one character at a time as the
broadcaster builds them off-screen, which looks exactly like the
garbled output we had before this rewrite.

Pipeline
--------
  1. TS demux: parse PAT (PID 0) → find PMT PID → parse PMT → find
     video PID (stream_type 0x02). Collect that PID's payload bytes.
     Demuxing matters: scanning the whole TS for 0x000001B2 hits
     audio PIDs and PSI tables and produces false matches.
  2. Inside video PES: scan for mpeg2video picture_start_code
     (0x00 0x00 0x01 0x00) to capture each picture's temporal_reference,
     then user_data_start_code (0x00 0x00 0x01 0xB2) followed by ATSC
     identifier 'GA94' and user_data_type_code 0x03 (cc_data).
  3. Reorder by temporal_reference at every group/sequence start so
     captions arrive in DISPLAY order, not the elementary-stream's
     DECODE order. MPEG-2 stores B-frames out of order so they can
     reference future P-frames; without this reorder, the I-frame's
     captions arrive before the B-frames that visually preceded it,
     and "THE" comes out scrambled as "ETH".
  4. cc_data entries: filter cc_type == 0 (NTSC field 1 = CC1/CC2),
     strip parity bits, drop invalid pairs.
  5. Run byte pairs through a CEA-608 channel/mode state machine
     and emit complete lines on EOC / CR.
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


# CEA-608-E Annex C: standard ASCII 0x20–0x7F with a few overrides for
# the accented chars common in Spanish-language captions.
CEA608_BASIC_OVERRIDES = {
    0x2A: "á", 0x5C: "é", 0x5E: "í", 0x5F: "ó", 0x60: "ú",
    0x7B: "ç", 0x7C: "÷", 0x7D: "Ñ", 0x7E: "ñ", 0x7F: "█",
}

# Special characters: prefix 0x11 (CC1) / 0x19 (CC2), second byte 0x30–0x3F.
CEA608_SPECIAL = "®°½¿™¢£♪à èâêîôû"


def decode_basic(b: int) -> str:
    """Map a printable CEA-608 byte (parity stripped) to a Unicode glyph.
    Returns "" for non-printable bytes."""
    b &= 0x7F
    if b in CEA608_BASIC_OVERRIDES:
        return CEA608_BASIC_OVERRIDES[b]
    if 0x20 <= b <= 0x7F:
        return chr(b)
    return ""


class CC608Decoder:
    """Stateful CEA-608 decoder for field 1 (CC1 + CC2 multiplexed).

    Mirrors a 1980s consumer line-21 decoder: pop-on captions appear
    all at once on EOC, roll-up captions emit one line per CR.
    """

    def __init__(self, write, target_channel: int = 1):
        self.write = write
        self.target = target_channel
        self.channel = 1            # last channel selected by a control code
        self.last_pair = None       # for doubled-control suppression
        self.mode = "pop"           # 'pop' | 'roll' | 'paint'
        self.buf_nd: list[str] = [] # non-displayed memory (pop-on accumulator)
        self.row: list[str] = []    # current row (roll-up / paint-on staging)

    def feed_pair(self, b1: int, b2: int) -> None:
        b1 &= 0x7F
        b2 &= 0x7F
        if b1 == 0 and b2 == 0:
            return  # null pair: idle field, no caption data this frame

        is_control = 0x10 <= b1 <= 0x1F
        if is_control:
            # Doubled-control suppression. CEA-608 transmits every
            # control pair twice on consecutive fields; the receiver
            # discards the second. Reset on suppress so a true third
            # repeat (rare, but legal) still gets through.
            if (b1, b2) == self.last_pair:
                self.last_pair = None
                return
            self.last_pair = (b1, b2)
            # Channel bit lives in byte 1, regardless of opcode family.
            self.channel = 2 if (b1 & 0x08) else 1
            if self.channel == self.target:
                self._handle_control(b1, b2)
            return

        # Printable bytes inherit the most-recent control's channel.
        # Drop them entirely if we're parked on the wrong channel —
        # this is what stops CC2 (Spanish) gibberish leaking in.
        self.last_pair = None
        if self.channel != self.target:
            return
        s = decode_basic(b1) + decode_basic(b2)
        if s:
            self._add_text(s)

    def _handle_control(self, b1: int, b2: int) -> None:
        # Misc control codes: 0x14 0x20–0x2F (also 0x15 as a duplicate
        # field-2-disambiguation form some encoders emit).
        if b1 in (0x14, 0x15) and 0x20 <= b2 <= 0x2F:
            cmd = b2
            if cmd == 0x20:                  # RCL: pop-on mode
                self.mode = "pop"
            elif cmd in (0x25, 0x26, 0x27):  # RU2/RU3/RU4: roll-up
                self._enter_rollup()
            elif cmd == 0x29:                # RDC: paint-on
                self.mode = "paint"
            elif cmd == 0x2C:                # EDM: erase displayed memory
                # We don't render a screen, so "clearing display" is a
                # no-op for stdout output. Anything pending in row /
                # buf_nd belongs to the next caption, not what's onscreen.
                pass
            elif cmd == 0x2D:                # CR: carriage return
                self._cr()
            elif cmd == 0x2E:                # ENM: erase non-displayed
                self.buf_nd.clear()
            elif cmd == 0x2F:                # EOC: end of caption (swap)
                self._eoc()
            return

        # Mid-row codes: 0x11 0x20–0x2F (color/italic/underline).
        # They occupy one screen cell; emit a space so word boundaries
        # survive style transitions like "<white>HELLO<yellow>WORLD".
        if b1 == 0x11 and 0x20 <= b2 <= 0x2F:
            self._add_text(" ")
            return

        # Special characters: 0x11 0x30–0x3F → ®°½¿™¢£♪ etc.
        if b1 == 0x11 and 0x30 <= b2 <= 0x3F:
            idx = b2 - 0x30
            if idx < len(CEA608_SPECIAL):
                self._add_text(CEA608_SPECIAL[idx])
            return

        # Extended characters: 0x12 / 0x13 + 0x20–0x3F. The standard
        # spec replaces the previously-written cell with an accented
        # form; we'd need cursor tracking to do that right. Skip.
        if b1 in (0x12, 0x13):
            return

        # Preamble Address Code: 0x10–0x17 with byte 2 in 0x40–0x7F.
        # Sets row + indent + style. We don't track positioning, but
        # a row change inside a caption needs a separator so words
        # don't slam together: "ROW1TEXTROW2TEXT" → "ROW1TEXT ROW2TEXT".
        if 0x10 <= b1 <= 0x17 and 0x40 <= b2 <= 0x7F:
            if (self.mode == "pop" and self.buf_nd) or \
               (self.mode != "pop" and self.row):
                self._add_text(" ")
            return

        # Tab offsets (0x17 0x21–0x23, 0x1F 0x21–0x23) and other rare
        # codes — ignored for plain text output.

    def _enter_rollup(self) -> None:
        # Switching from pop-on to roll-up: drop any half-built pop-on
        # caption so it doesn't surface incorrectly on the next CR.
        if self.mode == "pop":
            self.buf_nd.clear()
        self.mode = "roll"

    def _add_text(self, s: str) -> None:
        if not s:
            return
        if self.mode == "pop":
            self.buf_nd.append(s)
        elif self.mode == "paint":
            # Paint-on: text appears as it's received. Stream straight
            # to stdout, just like a real TV draws each cell.
            self.write(s)
        else:  # roll-up
            self.row.append(s)

    def _cr(self) -> None:
        # Roll-up CR: finishes the current row and "scrolls" — we just
        # emit the row.
        if self.row:
            line = "".join(self.row).strip()
            if line:
                self.write(line + "\n")
            self.row.clear()

    def _eoc(self) -> None:
        # Pop-on EOC: the broadcaster has finished building the caption
        # in non-displayed memory; swap it to "displayed" and emit the
        # whole thing at once. This is the difference between captions
        # appearing as readable lines vs. char-by-char gibberish.
        if self.buf_nd:
            text = "".join(self.buf_nd).strip()
            if text:
                self.write(text + "\n")
            self.buf_nd.clear()


class TSDemux:
    """Minimal MPEG-TS demuxer: just enough to find the video PID via
    PAT → PMT and yield that PID's payload bytes. Without demuxing,
    audio PIDs and PSI tables produce false 0x000001B2 matches that
    feed garbage into the CEA-608 state machine.
    """

    SYNC = 0x47
    PKT_LEN = 188

    def __init__(self) -> None:
        self.pmt_pid: int | None = None
        self.video_pid: int | None = None

    def feed(self, data: bytes):
        """Process raw TS bytes; yield video-PID payload chunks."""
        i = 0
        n = len(data)
        while i + self.PKT_LEN <= n:
            if data[i] != self.SYNC:
                i += 1
                continue
            payload = self._extract(data[i:i + self.PKT_LEN])
            if payload:
                yield payload
            i += self.PKT_LEN

    def _extract(self, pkt: bytes) -> bytes | None:
        # TS hdr: sync(8) tei(1) pusi(1) tp(1) pid(13) tsc(2) afc(2) cc(4)
        pusi = (pkt[1] >> 6) & 0x01
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        afc = (pkt[3] >> 4) & 0x03
        offset = 4
        if afc & 0x02:  # adaptation field present
            af_len = pkt[4]
            offset = 5 + af_len
        if not (afc & 0x01) or offset >= self.PKT_LEN:
            return None
        payload = pkt[offset:]

        if pid == 0:
            if pusi:
                self._parse_pat(self._strip_ptr(payload))
            return None
        if self.pmt_pid is not None and pid == self.pmt_pid:
            if pusi:
                self._parse_pmt(self._strip_ptr(payload))
            return None
        if self.video_pid is not None and pid == self.video_pid:
            return bytes(payload)
        return None

    @staticmethod
    def _strip_ptr(payload: bytes) -> bytes:
        if not payload:
            return b""
        ptr = payload[0]
        return payload[1 + ptr:]

    def _parse_pat(self, sec: bytes) -> None:
        if len(sec) < 12 or sec[0] != 0x00:
            return
        section_len = ((sec[1] & 0x0F) << 8) | sec[2]
        end = min(3 + section_len - 4, len(sec))  # exclude CRC32
        i = 8
        while i + 4 <= end:
            program_num = (sec[i] << 8) | sec[i + 1]
            pid = ((sec[i + 2] & 0x1F) << 8) | sec[i + 3]
            if program_num != 0:  # 0 is the network PID, skip
                self.pmt_pid = pid
                return
            i += 4

    def _parse_pmt(self, sec: bytes) -> None:
        if len(sec) < 12 or sec[0] != 0x02:
            return
        section_len = ((sec[1] & 0x0F) << 8) | sec[2]
        end = min(3 + section_len - 4, len(sec))
        prog_info_len = ((sec[10] & 0x0F) << 8) | sec[11]
        i = 12 + prog_info_len
        while i + 5 <= end:
            stream_type = sec[i]
            es_pid = ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]
            es_info_len = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
            if stream_type == 0x02:  # MPEG-2 video
                self.video_pid = es_pid
                return
            i += 5 + es_info_len


class VideoStreamScanner:
    """Scans MPEG-2 video stream bytes for cc_data, reorders byte pairs
    by display order (picture temporal_reference), and feeds them to a
    CC608Decoder.

    Why the reorder matters:
        MPEG-2 stores pictures in DECODE order so B-frames can reference
        future P-frames. Captions are written by the broadcaster in
        DISPLAY order. Feeding pairs to the CEA-608 decoder in decode
        order scrambles words — a GOP of [I@2, B@0, B@1, P@5, B@3, B@4]
        sends caption bytes in that order, so 'THE' (display 0,1,2)
        arrives as E,T,H = 'ETH'.

    What we do:
        - On every picture_start_code (0x00 0x00 0x01 0x00) we read the
          10-bit temporal_reference field and start a new per-picture
          byte-pair bucket.
        - On user_data_start_code (0x00 0x00 0x01 0xB2) we parse the
          ATSC cc_data section and append cc_type==0 pairs to the
          CURRENT picture's bucket.
        - On group_start_code (0xB8) or sequence_start_code (0xB3) — the
          GOP boundaries — we sort the buffered pictures by
          temporal_reference and feed them to the CEA-608 decoder in
          display order, then reset.

    This is the same reorder that ccextractor and consumer TVs perform.
    """

    def __init__(self, decoder: CC608Decoder) -> None:
        self.decoder = decoder
        self.gop: list[tuple[int, list[tuple[int, int]]]] = []
        self.cur_temp_ref: int | None = None
        self.cur_pairs: list[tuple[int, int]] = []
        self.max_pictures_buffered = 60  # safety bound (~2s @ 30fps)

    def feed(self, blob: bytes) -> int:
        """Parse a video-stream buffer, emit reordered pairs to the
        decoder. Returns the offset just past the last fully-consumed
        start code so the caller can keep an unparsed tail."""
        n = len(blob)
        if n < 8:
            return 0
        pos = 0
        last = 0
        while pos < n - 5:
            idx = blob.find(b"\x00\x00\x01", pos)
            if idx < 0 or idx + 4 >= n:
                return last
            code = blob[idx + 3]
            if code == 0x00:                       # picture_start_code
                if idx + 6 > n:
                    return last
                # temporal_reference: 10 bits, big-endian, top of byte 4
                # then top 2 bits of byte 5.
                tr = (blob[idx + 4] << 2) | (blob[idx + 5] >> 6)
                self._begin_picture(tr)
                pos = idx + 4
                last = idx + 4
            elif code == 0xB2:                     # user_data_start_code
                consumed = self._consume_userdata(blob, idx + 4)
                if consumed < 0:
                    return last
                pos = consumed
                last = consumed
            elif code in (0xB3, 0xB8):             # sequence_/group_start
                self._flush_gop()
                pos = idx + 4
                last = idx + 4
            else:
                pos = idx + 4
            # Safety: if the GOP gets weirdly long (no group_start_code
            # ever arrives), flush periodically so memory + latency
            # stay bounded.
            if len(self.gop) >= self.max_pictures_buffered:
                self._flush_gop()
        return last

    def finalize(self) -> None:
        """Flush any pending pictures (call at shutdown)."""
        self._flush_gop()

    def _begin_picture(self, tr: int) -> None:
        self._end_picture()
        self.cur_temp_ref = tr
        self.cur_pairs = []

    def _end_picture(self) -> None:
        if self.cur_temp_ref is not None:
            self.gop.append((self.cur_temp_ref, self.cur_pairs))
        self.cur_temp_ref = None
        self.cur_pairs = []

    def _flush_gop(self) -> None:
        self._end_picture()
        if not self.gop:
            return
        self.gop.sort(key=lambda p: p[0])
        for _, pairs in self.gop:
            for b1, b2 in pairs:
                self.decoder.feed_pair(b1, b2)
        self.gop = []

    def _consume_userdata(self, blob: bytes, i: int) -> int:
        """Parse one ATSC user_data section starting just past the
        0x000001B2 start code. Returns the byte offset after the
        section, or -1 if truncated (caller should keep tail and try
        again next pass)."""
        n = len(blob)
        if i + 5 > n:
            return -1
        if blob[i:i + 4] != b"GA94":
            return i
        i += 4
        ud_type = blob[i]
        i += 1
        if ud_type != 0x03:           # 0x03 == cc_data
            return i
        if i >= n:
            return -1
        # cc_data header: process_em_data_flag(1) + process_cc_data_flag(1)
        # + additional_data_flag(1) + cc_count(5)
        hdr = blob[i]
        process_cc = (hdr & 0x40) != 0
        cc_count = hdr & 0x1F
        i += 1
        i += 1                        # em_data byte (typically 0xFF)
        if i + cc_count * 3 > n:
            return -1
        for _ in range(cc_count):
            type_byte = blob[i]
            cc1 = blob[i + 1]
            cc2 = blob[i + 2]
            i += 3
            cc_valid = (type_byte >> 2) & 0x01
            cc_type = type_byte & 0x03
            # cc_type 0 = NTSC field 1 (CC1/CC2), 1 = NTSC field 2 (CC3/CC4),
            # 2/3 = DTVCC packet fragments (CEA-708 — out of scope here).
            if process_cc and cc_valid and cc_type == 0:
                if self.cur_temp_ref is not None:
                    self.cur_pairs.append((cc1, cc2))
                else:
                    # cc_data outside a picture context — feed direct.
                    # Shouldn't happen in well-formed streams.
                    self.decoder.feed_pair(cc1, cc2)
        return i


def tail_follow(path: Path, chunk_size: int = 64 * 1024,
                startup_timeout: float = 30.0):
    """Yield byte chunks from `path` as the file grows. Waits for the
    file to appear at startup."""
    deadline = time.time() + startup_timeout
    while not path.exists() and time.time() < deadline:
        time.sleep(0.5)
    if not path.exists():
        raise FileNotFoundError(f"{path} did not appear within "
                                f"{startup_timeout} s")
    f = open(path, "rb")
    try:
        # Start near the end so we get LIVE captions, not the backlog.
        # 256 KB ≈ 100 ms of TS — guaranteed to land on a packet.
        size = path.stat().st_size
        f.seek(max(0, size - 256 * 1024))
        first = True
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                time.sleep(0.5)
                continue
            if first:
                # Re-align to a TS sync byte on the first read so the
                # demuxer doesn't waste packets resyncing.
                sync = chunk.find(b"\x47")
                if sync > 0:
                    chunk = chunk[sync:]
                first = False
            yield chunk
    finally:
        f.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Pure-Python CEA-608 closed-caption decoder.")
    ap.add_argument("ts_path", nargs="?",
                    help="Path to live MPEG-TS (default: tv_live's live.ts)")
    ap.add_argument("--channel", type=int, default=1, choices=[1, 2],
                    help="Which CEA-608 channel to emit (1=primary, 2=SAP). "
                         "Default 1 (English).")
    args = ap.parse_args()

    if args.ts_path:
        path = Path(args.ts_path)
    else:
        here = Path(__file__).resolve().parent
        candidates = [
            here.parent / "data" / "tv_live" / "live.ts",
            here / "data" / "tv_live" / "live.ts",
        ]
        path = next((c for c in candidates if c.exists()), candidates[0])

    print(f"[atsc_cc] reading captions from {path}", file=sys.stderr)
    print(f"[atsc_cc] CEA-608 field 1 / channel {args.channel} only.",
          file=sys.stderr)
    print("[atsc_cc] If captions don't appear, the broadcaster may not be "
          "transmitting them.\n", file=sys.stderr)

    def write_text(s: str) -> None:
        sys.stdout.write(s)
        sys.stdout.flush()

    decoder = CC608Decoder(write_text, target_channel=args.channel)
    demux = TSDemux()
    scanner = VideoStreamScanner(decoder)
    video_buf = bytearray()
    announced_video_pid = False

    try:
        for chunk in tail_follow(path):
            for vp in demux.feed(chunk):
                video_buf.extend(vp)
            if (not announced_video_pid and demux.video_pid is not None):
                print(f"[atsc_cc] locked video PID 0x{demux.video_pid:04X}",
                      file=sys.stderr)
                announced_video_pid = True
            # Scan when there's enough to chew on; keep an overlap tail
            # so user_data sections that straddle chunks parse on the
            # next pass.
            if len(video_buf) >= 8192:
                consumed = scanner.feed(bytes(video_buf))
                if consumed > 0:
                    del video_buf[:consumed]
                # Cap the buffer if the broadcaster is sending no CCs,
                # so memory usage stays bounded.
                if len(video_buf) > 256 * 1024:
                    del video_buf[:-1024]
    except KeyboardInterrupt:
        pass
    except FileNotFoundError as e:
        print(f"[atsc_cc] {e}", file=sys.stderr)
        return 1
    finally:
        scanner.finalize()
    return 0


if __name__ == "__main__":
    sys.exit(main())
