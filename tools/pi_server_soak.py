#!/usr/bin/env python3
"""Overnight soak-test of the Pi's soapyremote-server — the split's front-end.

Pulls IQ from the LOCAL server continuously for SOAK_HOURS, RECONNECTING every
SOAK_RECONNECT_MIN minutes. The reconnect is the point: it exercises exactly what
the Ryzen does when its decoder restarts, and catches the failure mode where a
client teardown wedges the SDRplay API (see memory sdrplay-graceful-shutdown).

Per interval it logs: effective delivery MS/s (vs 8.0), server RSS (memory-leak
check), and temp/throttle. On a failed (re)connect it restarts the service and
retries — logging the event — so one wedge doesn't end the night.

Env: SOAK_HOURS (7), SOAK_RECONNECT_MIN (20), SOAK_LOG, SOAK_FREQ_HZ (479e6).
Stop cleanly any time with SIGINT/SIGTERM.
"""
import os, sys, time, subprocess, signal
from gnuradio import gr, soapy, blocks

HOURS         = float(os.environ.get("SOAK_HOURS", "7"))
RECONNECT_MIN = float(os.environ.get("SOAK_RECONNECT_MIN", "20"))
FREQ          = float(os.environ.get("SOAK_FREQ_HZ", "479e6"))
RATE          = 8e6
DEV           = "driver=remote,remote=127.0.0.1:55132,remote:driver=sdrplay"
STREAM        = "remote:prot=tcp"
LOG           = os.environ.get("SOAK_LOG", os.path.expanduser("~/pi_autobot/soak.log"))

os.makedirs(os.path.dirname(LOG), exist_ok=True)
_stop = False
def _sig(*_):
    global _stop; _stop = True
signal.signal(signal.SIGINT, _sig)
signal.signal(signal.SIGTERM, _sig)

def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [soak] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f: f.write(line + "\n")

def sh(cmd):
    try: return subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL).decode().strip()
    except Exception: return ""

def server_rss_kb():
    pid = sh("pgrep -x SoapySDRServer | head -1")
    if not pid: return -1
    try: return int(open(f"/proc/{pid}/status").read().split("VmRSS:")[1].split()[0])
    except Exception: return -1

def restart_server():
    log("restarting soapyremote-server (recovery)")
    sh("sudo systemctl restart soapyremote-server")
    time.sleep(6)

class Puller(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, catch_exceptions=False)
        self.src = soapy.source(DEV, "fc32", 1, "", STREAM, [""], [""])
        self.src.set_sample_rate(0, RATE)
        self.src.set_frequency(0, FREQ)
        try: self.src.set_gain_mode(0, False)
        except Exception: pass
        self.snk = blocks.null_sink(gr.sizeof_gr_complex)
        self.connect(self.src, self.snk)

def run_session(max_secs):
    """One connect→pull→disconnect cycle. Returns True if it streamed."""
    try:
        tb = Puller(); tb.start()
    except Exception as e:
        log(f"ERROR connect failed: {e}")
        return False
    last, t_last, t0 = 0, time.time(), time.time()
    streamed = False
    while not _stop and (time.time() - t0) < max_secs:
        time.sleep(30)
        now = tb.snk.nitems_read(0)
        dt = max(1e-3, time.time() - t_last)
        rate = (now - last) / dt / 1e6
        if now > 0: streamed = True
        rss = server_rss_kb()
        tt  = sh("vcgencmd measure_temp").replace("temp=", "")
        thr = sh("vcgencmd get_throttled").replace("throttled=", "")
        flag = "" if thr in ("0x0", "") else "  <<THROTTLE>>"
        log(f"rate={rate:5.2f} MS/s ({rate/8*100:3.0f}%)  srv_rss={rss}KB  {tt}  thr={thr}{flag}")
        last, t_last = now, time.time()
    try:
        tb.stop(); tb.wait()   # graceful — never kill -9 (wedges SDRplay)
    except Exception as e:
        log(f"warn: teardown {e}")
    return streamed

def main():
    log(f"=== START soak: {HOURS}h, reconnect every {RECONNECT_MIN}min, server={DEV} ===")
    end = time.time() + HOURS*3600
    rss0 = server_rss_kb(); log(f"server RSS at start: {rss0}KB")
    session, fails = 0, 0
    while not _stop and time.time() < end:
        session += 1
        remaining = end - time.time()
        ok = run_session(min(RECONNECT_MIN*60, remaining))
        if _stop: break
        if not ok:
            fails += 1
            log(f"session {session} delivered NOTHING (fail #{fails}) — recovering")
            restart_server()
        else:
            log(f"--- session {session} done; reconnecting (clean client restart) ---")
        time.sleep(2)
    rssN = server_rss_kb()
    log(f"=== END soak: {session} sessions, {fails} failed reconnects, "
        f"server RSS {rss0}->{rssN}KB (leak check) ===")

if __name__ == "__main__":
    main()
