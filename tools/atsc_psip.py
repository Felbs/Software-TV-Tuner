"""Minimal ATSC PSIP parser.

Decodes Virtual Channel Table (TVCT/CVCT, table_id 0xC8/0xC9) and
Event Information Table (EIT-0..3, table_id 0xCB..0xCE) directly from
a captured MPEG-TS file. Standalone — uses only stdlib.

References:
  * ATSC A/65 — Program and System Information Protocol for Terrestrial
    Broadcast and Cable.
  * ISO/IEC 13818-1 — MPEG-2 Transport Stream / PSI section syntax.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

PSIP_BASE_PID = 0x1FFB
TABLE_MGT = 0xC7   # Master Guide Table — points to EIT PIDs
TABLE_TVCT = 0xC8
TABLE_CVCT = 0xC9
TABLE_EIT = 0xCB   # all EIT-N variants share this table_id; PID differs

# GPS epoch = 1980-01-06 00:00:00 UTC. PSIP times are GPS seconds with a
# constant 18-second offset versus UTC (correct as of 2017; GPS doesn't
# track leap seconds, UTC does).
GPS_EPOCH = datetime.datetime(1980, 1, 6, tzinfo=datetime.timezone.utc)
GPS_UTC_LEAP_SECONDS = 18


def parse_ts_packets(buf: bytes, pid_filter: int):
    """Yield (payload_unit_start, payload_bytes) for each TS packet
    in `buf` whose PID matches `pid_filter`. Skips packets with errors
    or scrambling. The TS header and any adaptation field are stripped."""
    n = len(buf) // 188
    for i in range(n):
        pkt = buf[i * 188:(i + 1) * 188]
        if pkt[0] != 0x47:
            continue
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pid != pid_filter:
            continue
        if pkt[1] & 0x80:        # transport_error_indicator
            continue
        if pkt[3] & 0xC0:        # transport_scrambling_control != 0
            continue
        adapt_ctrl = (pkt[3] >> 4) & 0x3
        idx = 4
        if adapt_ctrl in (2, 3):
            ad_len = pkt[4]
            idx = 5 + ad_len
        if adapt_ctrl in (1, 3) and idx < 188:
            pus = (pkt[1] & 0x40) != 0
            yield pus, bytes(pkt[idx:])


def reassemble_sections(packet_iter):
    """Reassemble PSI sections from a stream of (pus, payload) tuples for
    one PID. Yields each complete section as bytes. PSI sections span
    multiple TS packets and are framed with a pointer_field on packets
    where payload_unit_start_indicator=1."""
    buf = b""
    pending = 0  # 0 means not currently inside a section being assembled
    for pus, payload in packet_iter:
        if pus:
            if not payload:
                continue
            ptr = payload[0]
            # Tail of any in-flight section.
            if pending > 0:
                tail = payload[1:1 + min(ptr, pending - len(buf))]
                buf += tail
                if len(buf) >= pending:
                    yield buf[:pending]
                buf = b""
                pending = 0
            payload = payload[1 + ptr:]
            while len(payload) >= 3:
                section_len = ((payload[1] & 0x0F) << 8) | payload[2]
                total = section_len + 3
                if total < 4 or total > 1024:
                    break  # malformed
                if len(payload) >= total:
                    yield payload[:total]
                    payload = payload[total:]
                else:
                    buf = payload
                    pending = total
                    break
        else:
            if pending > 0:
                buf += payload
                if len(buf) >= pending:
                    yield buf[:pending]
                    buf = b""
                    pending = 0


def parse_atsc_text(data: bytes) -> str:
    """Decode an ATSC multiple_string_structure (A/65 §6.10) to plain
    text. Supports the common case: one string, one segment, no
    compression, mode 0x00 (basic Latin) or 0x3F (UTF-16 BE direct).
    Returns "" on parse failure or unsupported encoding."""
    try:
        if len(data) < 1:
            return ""
        n_strings = data[0]
        if n_strings < 1:
            return ""
        idx = 1
        # First string only.
        if idx + 3 > len(data):
            return ""
        idx += 3  # skip ISO_639 language code
        if idx >= len(data):
            return ""
        n_segs = data[idx]
        idx += 1
        if n_segs < 1:
            return ""
        # First segment.
        if idx + 3 > len(data):
            return ""
        compression_type = data[idx]
        mode = data[idx + 1]
        n_bytes = data[idx + 2]
        idx += 3
        seg = data[idx:idx + n_bytes]
        if compression_type != 0x00:
            return ""
        if mode == 0x00:
            # 16-bit Unicode with high byte = 0x00 → equivalent to ASCII.
            return seg.decode("ascii", errors="replace").strip()
        if mode == 0x3F:
            return seg.decode("utf-16-be", errors="replace").strip()
        return ""
    except Exception:
        return ""


def parse_vct_section(section: bytes):
    """Parse a TVCT/CVCT section, return list of channel dicts."""
    if len(section) < 11:
        return []
    table_id = section[0]
    if table_id not in (TABLE_TVCT, TABLE_CVCT):
        return []
    num_channels = section[9]
    idx = 10
    channels = []
    for _ in range(num_channels):
        if idx + 32 > len(section):
            break
        short_name = section[idx:idx + 14].decode(
            "utf-16-be", errors="replace").rstrip("\x00").strip()
        idx += 14
        major = ((section[idx] & 0x0F) << 6) | (section[idx + 1] >> 2)
        minor = ((section[idx + 1] & 0x03) << 8) | section[idx + 2]
        idx += 3
        idx += 1  # modulation_mode
        idx += 4  # carrier_frequency
        idx += 2  # channel_TSID
        program_number = int.from_bytes(section[idx:idx + 2], "big")
        idx += 2
        idx += 2  # ETM_location/access/hidden/.../service_type packed
        source_id = int.from_bytes(section[idx:idx + 2], "big")
        idx += 2
        descriptors_length = ((section[idx] & 0x03) << 8) | section[idx + 1]
        idx += 2
        idx += descriptors_length
        channels.append({
            "short_name": short_name,
            "major": major,
            "minor": minor,
            "program_number": program_number,
            "source_id": source_id,
        })
    return channels


def parse_mgt_section(section: bytes) -> list[dict]:
    """Parse a Master Guide Table section. Returns list of table refs:
    {"table_type": int, "pid": int}. EIT-N has table_type 0x0100..0x017F."""
    if len(section) < 13 or section[0] != TABLE_MGT:
        return []
    tables_count = int.from_bytes(section[9:11], "big")
    idx = 11
    out = []
    for _ in range(tables_count):
        if idx + 11 > len(section):
            break
        table_type = int.from_bytes(section[idx:idx + 2], "big")
        idx += 2
        pid = ((section[idx] & 0x1F) << 8) | section[idx + 1]
        idx += 2
        idx += 1  # version
        idx += 4  # number_bytes
        descriptors_length = ((section[idx] & 0x0F) << 8) | section[idx + 1]
        idx += 2
        idx += descriptors_length
        out.append({"table_type": table_type, "pid": pid})
    return out


def parse_eit_section(section: bytes):
    """Parse an EIT-N section, return (source_id, [event...])."""
    if len(section) < 11:
        return None, []
    table_id = section[0]
    if table_id != TABLE_EIT:
        return None, []
    source_id = int.from_bytes(section[3:5], "big")
    num_events = section[9]
    idx = 10
    events = []
    for _ in range(num_events):
        if idx + 12 > len(section):
            break
        event_id = ((section[idx] & 0x3F) << 8) | section[idx + 1]
        idx += 2
        start_time = int.from_bytes(section[idx:idx + 4], "big")
        idx += 4
        # 3 bytes: reserved(2) + ETM_location(2) + length_in_seconds(20)
        etm_and_len = int.from_bytes(section[idx:idx + 3], "big")
        length_sec = etm_and_len & 0x000FFFFF
        idx += 3
        title_length = section[idx]
        idx += 1
        title = parse_atsc_text(section[idx:idx + title_length])
        idx += title_length
        if idx + 2 > len(section):
            break
        descriptors_length = ((section[idx] & 0x0F) << 8) | section[idx + 1]
        idx += 2
        idx += descriptors_length
        events.append({
            "event_id": event_id,
            "start_gps": start_time,
            "length_sec": length_sec,
            "title": title,
        })
    return source_id, events


def gps_to_datetime(gps_sec: int) -> datetime.datetime:
    return GPS_EPOCH + datetime.timedelta(
        seconds=gps_sec - GPS_UTC_LEAP_SECONDS)


def extract_psip(ts_path: Path, max_bytes: int = 100_000_000) -> dict:
    """Read up to max_bytes from `ts_path`, decode TVCT, MGT, and EIT.

    Two-pass: first pass over PID 0x1FFB extracts TVCT (channel list)
    and MGT (which tells us the PIDs that carry EIT). Second pass over
    each EIT PID extracts the event lists.

    Returns {"channels": [...], "events": {source_id: [event...]}}.
    Best-effort: any parse failure for one section is skipped silently."""
    if not Path(ts_path).exists():
        return {"channels": [], "events": {}}
    with open(ts_path, "rb") as f:
        data = f.read(max_bytes)

    channels = []
    eit_pids: set[int] = set()
    for section in reassemble_sections(parse_ts_packets(data, PSIP_BASE_PID)):
        if len(section) < 4:
            continue
        table_id = section[0]
        if table_id in (TABLE_TVCT, TABLE_CVCT):
            channels.extend(parse_vct_section(section))
        elif table_id == TABLE_MGT:
            for ref in parse_mgt_section(section):
                # EIT-N table_type values: 0x0100..0x017F per A/65 §6.3.
                if 0x0100 <= ref["table_type"] <= 0x017F:
                    eit_pids.add(ref["pid"])

    events_by_source: dict[int, list[dict]] = {}
    for pid in eit_pids:
        for section in reassemble_sections(parse_ts_packets(data, pid)):
            if len(section) < 4 or section[0] != TABLE_EIT:
                continue
            sid, evs = parse_eit_section(section)
            if sid is not None and evs:
                events_by_source.setdefault(sid, []).extend(evs)
    # Dedupe channels by source_id (multiple sections can repeat them).
    seen = set()
    deduped = []
    for c in channels:
        if c["source_id"] in seen:
            continue
        seen.add(c["source_id"])
        deduped.append(c)
    # Dedupe events by event_id within each source.
    for sid, evs in events_by_source.items():
        seen_e: dict[int, dict] = {}
        for e in evs:
            seen_e.setdefault(e["event_id"], e)
        events_by_source[sid] = sorted(
            seen_e.values(), key=lambda e: e["start_gps"])
    return {"channels": deduped, "events": events_by_source}


def find_current_event(events: list,
                       now: datetime.datetime | None = None) -> dict | None:
    """Find the event whose [start, start+length) contains `now`."""
    if not events:
        return None
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    for ev in events:
        start = gps_to_datetime(ev["start_gps"])
        end = start + datetime.timedelta(seconds=ev["length_sec"])
        if start <= now < end:
            remaining = max(0, int((end - now).total_seconds()))
            return {
                "title": ev["title"],
                "start_iso": start.isoformat(),
                "duration_sec": ev["length_sec"],
                "remaining_sec": remaining,
            }
    return None


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if len(sys.argv) < 2:
        print("usage: python atsc_psip.py <ts_file>")
        sys.exit(1)
    info = extract_psip(Path(sys.argv[1]))
    print(f"Channels ({len(info['channels'])}):")
    for c in info["channels"]:
        print(f"  source={c['source_id']:>5}  "
              f"{c['major']}.{c['minor']:<3}  "
              f"prog#={c['program_number']:<5}  "
              f"name={c['short_name']!r}")
    print()
    print(f"Events by source_id:")
    for sid, evs in info["events"].items():
        print(f"  source={sid}:")
        for e in evs[:3]:
            t = gps_to_datetime(e["start_gps"]).strftime("%H:%M")
            print(f"    {t}  ({e['length_sec']//60:>3}min)  {e['title']!r}")
        if len(evs) > 3:
            print(f"    ... +{len(evs)-3} more")
    now_ev = {sid: find_current_event(evs)
              for sid, evs in info["events"].items()}
    print()
    print("Currently airing:")
    for sid, ev in now_ev.items():
        if ev:
            mins = ev["remaining_sec"] // 60
            print(f"  source={sid}: {ev['title']!r} "
                  f"({mins} min remaining)")
