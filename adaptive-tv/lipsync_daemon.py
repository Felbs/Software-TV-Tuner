"""lipsync_daemon.py — closed-loop lip-sync + caption hygiene sidecar.

Talks to the running player over the fixed IPC socket:
  - every 20 s: sample mpv's A/V offset a few times; if the median is
    off by >0.12 s, nudge audio-delay by half the error (gentle servo,
    clamped to +/-2 s). Positive A-V = audio ahead -> positive delay.
  - every 8 min: cycle the caption track (clears stuck/frozen 608 state).
Survives player restarts (socket name is constant; failed reads just wait).
Log tokens: SYNC (adjustments) / CC-CYCLE
"""
import json, statistics, time

IPC = r"\\.\pipe\mpv-tvtuna-super"

def get(prop):
    try:
        with open(IPC, "r+b", buffering=0) as p:
            p.write(json.dumps({"command": ["get_property", prop],
                                "request_id": 9}).encode() + b"\n")
            t0 = time.time(); buf = b""
            while time.time() - t0 < 2:
                buf += p.read(4096)
                for line in buf.split(b"\n"):
                    if not line.strip(): continue
                    try: r = json.loads(line)
                    except ValueError: continue
                    if r.get("request_id") == 9:
                        return r.get("data")
    except OSError:
        return None
    return None

def setp(prop, val):
    try:
        with open(IPC, "wb") as p:
            p.write(json.dumps({"command": ["set_property", prop, val]}
                               ).encode() + b"\n")
        return True
    except OSError:
        return False

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

log("lipsync daemon up")
last_cc = time.time()
while True:
    samples = []
    for _ in range(5):
        v = get("avsync")
        if isinstance(v, (int, float)): samples.append(v)
        time.sleep(1.5)
    if len(samples) >= 3:
        med = statistics.median(samples)
        if abs(med) > 0.12:
            cur = get("audio-delay") or 0.0
            new = max(-2.0, min(2.0, cur + 0.5 * med))
            if abs(new - cur) > 0.02 and setp("audio-delay", round(new, 3)):
                log(f"SYNC A-V={med:+.3f}s audio-delay {cur:+.3f} -> {new:+.3f}")
    if time.time() - last_cc > 480:
        if setp("sid", "no"):
            time.sleep(0.5); setp("sid", 1)
            log("CC-CYCLE")
        last_cc = time.time()
    time.sleep(12)
