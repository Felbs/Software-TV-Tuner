"""mer_meter.py — live MER (Modulation Error Ratio) from the chain's own equalizer.

THE missing metric between "pilot locks" (mean|x|) and "video decodes" (seq-headers).
The long equalizer computes fs_err_rms (RMS error vs the known field-sync training
sequence) every field sync — that IS the MER the broadcast industry uses:

    MER_dB ~= 20*log10(5.0 / fs_err_rms)      (training symbols are +/-5)

The ATSC 8-VSB data cliff is ~15.2 dB. This meter shows continuously how far the
signal is from decodable — a GRADIENT for aiming/gain instead of a binary cliff.

Modes:
    python mer_meter.py --rf 31                          # live dashboard
    python mer_meter.py --rf 31 --tone                   # aim-by-ear (Bluetooth-safe
                                                         # continuous tone, pitch = MER)
Ctrl-C to stop. Telemetry enabled via STVT_EQ_TELEM=1 (zero overhead otherwise).
"""
import argparse, math, os, re, subprocess, sys, time
from pathlib import Path

PY = r"C:\Users\user\radioconda\python.exe"
TV_LIVE = Path(r"Z:\src\magic-tv-decoder\tools\tv_live.py")
LIVE = Path(r"Z:\src\magic-tv-decoder\tools\data\tv_live\live.ts")
SDRPLAY_DLL = r"C:\Program Files\SDRplay\API\x64"
LOG = Path(os.environ.get("TEMP", ".")) / "mer_meter_chain.log"

CLIFF_DB = 15.2                     # 8-VSB TOV (threshold of visibility)
TRAIN_RMS = 5.0                     # field-sync training symbols are +/-5

RE_FS = re.compile(r"fs_err_rms=([\d.]+)")
RE_FPLL = re.compile(r"mean\|x\|=([\d.]+).*?in_rms=([\d.]+)")

# tone map: MER 8 dB -> 220 Hz ... 20 dB -> 1320 Hz (cliff 15.2 ~= 880 Hz)
LO_DB, HI_DB = 8.0, 20.0
LO_HZ, HI_HZ = 220.0, 1320.0


def chain_env(args):
    e = os.environ.copy()
    e["PATH"] = SDRPLAY_DLL + os.pathsep + e.get("PATH", "")
    e.update({
        "STVT_ANTENNA": args.antenna, "STVT_IFGR": str(args.ifgr),
        "STVT_RFGAIN_SEL": str(args.rfgain), "STVT_EQ": "long",
        "STVT_VITERBI": "soft", "STVT_RFNOTCH": "1", "STVT_DABNOTCH": "1",
        "STVT_RS": "stock", "STVT_SPS": "1.1", "STVT_RRC_SYMS": "8",
        "STVT_TEISCRUB": "1", "STVT_EQ_LKG": "1", "STVT_EQ_LKG_RMS": "1.0",
        "STVT_EQ_TELEM": "1",                       # <-- the whole point
    })
    if args.biast:
        e["STVT_BIAST"] = "1"
    return e


def mer_db(err_rms: float) -> float:
    if err_rms <= 0: return HI_DB
    return 20.0 * math.log10(TRAIN_RMS / err_rms)


class HeaderCounter:
    """Incremental seq-header counter — reads only NEW bytes of live.ts."""
    def __init__(self):
        self.off = 0; self.carry = b""

    def new_headers(self) -> int:
        if not LIVE.exists(): self.off = 0; self.carry = b""; return 0
        try: size = LIVE.stat().st_size
        except OSError: return 0
        if size < self.off: self.off = 0; self.carry = b""   # rotated
        if size == self.off: return 0
        try:
            with open(LIVE, "rb") as f:
                f.seek(self.off)
                data = self.carry + f.read(size - self.off)
        except OSError:
            return 0
        self.off = size
        self.carry = data[-3:]
        return data.count(b"\x00\x00\x01\xb3")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rf", type=int, default=31)
    ap.add_argument("--antenna", default="Antenna A")
    ap.add_argument("--ifgr", type=int, default=40)
    ap.add_argument("--rfgain", type=int, default=3)
    ap.add_argument("--tone", action="store_true", help="aim-by-ear continuous tone")
    ap.add_argument("--biast", action="store_true", help="power a bias-T LNA")
    ap.add_argument("--window", type=int, default=0, help="stop after N s (0=forever)")
    args = ap.parse_args()

    tone = None
    if args.tone:
        import numpy as np
        import sounddevice as sd

        class Tone:
            def __init__(self):
                self._target = LO_HZ; self._cur = LO_HZ; self._phase = 0.0
                self.stream = sd.OutputStream(samplerate=44100, channels=1,
                                              blocksize=1024, callback=self._cb)
            def set_freq(self, hz): self._target = float(hz)
            def _cb(self, outdata, frames, t, status):
                out = np.empty(frames, dtype=np.float32)
                for i in range(frames):
                    self._cur += (self._target - self._cur) * 0.0008
                    self._phase += 2 * np.pi * self._cur / 44100
                    if self._phase > 2 * np.pi: self._phase -= 2 * np.pi
                    out[i] = 0.18 * np.sin(self._phase)
                outdata[:, 0] = out
            def start(self): self.stream.start()
            def stop(self): self.stream.stop(); self.stream.close()
        tone = Tone(); tone.start()

    print(f"  MER meter: RF{args.rf} {args.antenna} IFGR={args.ifgr} rfgain={args.rfgain}")
    print(f"  data cliff = {CLIFF_DB} dB.  ABOVE cliff = decodable TV."
          + ("  (tone: 880 Hz = cliff, higher = better)" if args.tone else ""))
    print(f"  {'t':>5} {'MER dB':>7} {'margin':>7} {'mean|x|':>8} {'in_rms':>7} "
          f"{'hdr/s':>6}  bar")

    if LIVE.exists():
        try: LIVE.unlink()
        except OSError: pass
    lf = open(LOG, "w")
    ch = subprocess.Popen([PY, "-u", str(TV_LIVE), "--rf", str(args.rf)],
                          env=chain_env(args), stdout=lf, stderr=subprocess.STDOUT)
    hdr = HeaderCounter()
    log_off = 0
    mer_s = None                    # EMA-smoothed MER
    t0 = time.time()
    try:
        while ch.poll() is None:
            time.sleep(1.0)
            t = time.time() - t0
            if args.window and t > args.window: break
            try:
                with open(LOG, "r", errors="ignore") as f:
                    f.seek(log_off); chunk = f.read(); log_off = f.tell()
            except OSError:
                chunk = ""
            errs = [float(m.group(1)) for m in RE_FS.finditer(chunk)]
            fpll = RE_FPLL.findall(chunk)
            mx = float(fpll[-1][0]) if fpll else 0.0
            irms = float(fpll[-1][1]) if fpll else 0.0
            h = hdr.new_headers()
            if errs:
                m_now = sum(mer_db(e) for e in errs) / len(errs)
                mer_s = m_now if mer_s is None else mer_s + (m_now - mer_s) * 0.5
            if mer_s is None:
                line = (f"  {t:5.0f} {'--':>7} {'--':>7} {mx:8.4f} {irms:7.1f} "
                        f"{h:6d}  (no field-sync telemetry yet)")
                if tone: tone.set_freq(LO_HZ)
            else:
                margin = mer_s - CLIFF_DB
                frac = max(0.0, min(1.0, (mer_s - LO_DB) / (HI_DB - LO_DB)))
                nbar = int(frac * 34)
                cliff_pos = int((CLIFF_DB - LO_DB) / (HI_DB - LO_DB) * 34)
                bar = "".join("|" if i == cliff_pos else
                              ("#" if i < nbar else ".") for i in range(34))
                tag = "DECODE" if margin >= 0 else f"{margin:+.1f}"
                line = (f"  {t:5.0f} {mer_s:7.2f} {tag:>7} {mx:8.4f} {irms:7.1f} "
                        f"{h:6d}  {bar}")
                if tone: tone.set_freq(LO_HZ + frac * (HI_HZ - LO_HZ))
            print(line); sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        if tone: tone.stop()
        ch.terminate()
        try: ch.wait(timeout=6)
        except Exception: ch.kill()
        lf.close()
        if mer_s is not None:
            print(f"\n  final MER {mer_s:.2f} dB "
                  f"({mer_s - CLIFF_DB:+.2f} dB vs cliff {CLIFF_DB})")


if __name__ == "__main__":
    main()
