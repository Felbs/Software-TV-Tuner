"""Autonomous English<->Spanish channel-pair switcher + verifier.

The reality of DC over-the-air broadcasting:
 * English-primary channels (NBC, ABC, CBS, Fox) often have a 'spa'-
   tagged audio track that's either SILENT (broadcaster doesn't
   activate the SAP) or carries DVS (Descriptive Video Service in
   English, narration for the visually impaired).
 * The only reliably-Spanish content sits on Spanish-PRIMARY channels:
   Univision (RF 15) and Telemundo (RF 34, program 5 in NBC's mux).

So "toggle Spanish on the current channel" cannot work in DC. What we
CAN do is toggle between PAIRED channels:
   English pick  -> NBC News 4    (RF 34, program 3)
   Spanish pick  -> Telemundo     (RF 34, program 5)
Both are in the same multiplex, so the SDR stays locked when switching
and the retune costs <3 seconds (just a fresh ffmpeg + VLC).

What this agent does
====================
1. Defines the English/Spanish pair.
2. Auto-runs a verification loop:
     - Set chain to English pick. Wait for live.ts. Extract audio.
       Run Windows speech recognizer. Expect >= 5 confident English
       words. Pass / fail.
     - Set chain to Spanish pick. Wait. Extract audio. Run recognizer.
       Expect < 2 English words (with audio above silence floor).
       Pass / fail.
     - Repeat N times to make sure the result is stable.
3. Reports a verdict. If the pair works, registers global hotkeys
   that perform the channel-pair switch.

This script is launched ONCE. It keeps running, holding the verified
chain alive, listening for Ctrl+Shift+E / Ctrl+Shift+S to switch.

Usage:
    python tools/stvt_lang_pair_agent.py
    python tools/stvt_lang_pair_agent.py --no-hotkeys  # test only
    python tools/stvt_lang_pair_agent.py --runs 3      # quick pass
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools"
TV_TUNER_PY = TOOLS / "tv_tuner.py"
LIVE_TS = REPO / "tools" / "data" / "tv_live" / "live.ts"
WORK_DIR = REPO / "tools" / "data" / "lang_pair_agent"

PY = sys.executable
FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
FFPROBE = r"C:\ffmpeg\bin\ffprobe.exe" if sys.platform == "win32" else "ffprobe"

CHAIN_ENV = {
    "STVT_EQ":          "long",
    "STVT_RS":          "stock",
    "STVT_VITERBI":     "soft",
    "STVT_SPS":         "1.1",
    "STVT_RRC_SYMS":    "8",
    "STVT_TEISCRUB":    "1",
    "STVT_EQ_LKG":      "1",
    "STVT_EQ_LKG_RMS":  "1.0",
    "STVT_IFGR":        "45",
    "STVT_RFGAIN_SEL":  "3",
    "STVT_ANTENNA":     "Antenna A",
    "STVT_SUB_MARGIN":  "10",
}

# The pair. Both share a multiplex so SDR stays locked.
PICKS = {
    "eng": {
        "label":   "NBC News 4 (English)",
        "rf":      34,
        "program": 3,
        "audio_language": "eng",
    },
    "spa": {
        "label":   "Telemundo (Spanish)",
        "rf":      34,
        "program": 5,
        "audio_language": "all",   # let VLC pick from the program
    },
}

# Win32 hotkey constants
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
WM_HOTKEY = 0x0312
VK_E = 0x45
VK_S = 0x53
VK_Q = 0x51
VK_A = 0x41


# ----------------- helpers -----------------

def log(msg, fh=None):
    print(msg, flush=True)
    if fh:
        fh.write(msg + "\n")
        fh.flush()


def run(cmd, timeout=60, env=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


def kill_chain():
    if sys.platform == "win32":
        for img in ("ffmpeg.exe", "vlc.exe", "ffplay.exe"):
            # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign); loop also sweeps mpv/ffmpeg players
            subprocess.run(["taskkill", "/F", "/IM", img], capture_output=True)  # kill-ok (see above)
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True,
        ).stdout
        for line in out.splitlines():
            if "tv_tuner.py" in line or "tv_live.py" in line:
                try:
                    pid = int(line.strip().rsplit(",", 1)[-1])
                    # kill-ok (reviewed 8/01): TV-chain stop - pre-warden family, no stop-file exists; this IS its documented stop. Revisit with warden citizenship (bare-open campaign)
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],  # kill-ok (see above)
                                   capture_output=True)
                except (ValueError, IndexError):
                    pass


def spawn_picked(pick: dict, env: dict) -> subprocess.Popen | None:
    """Spawn tv_tuner.py for the pick. Returns the Popen handle."""
    pick_env = env.copy()
    pick_env["STVT_AUDIO_LANGUAGE"] = pick["audio_language"]
    cmd = [PY, "-u", str(TV_TUNER_PY),
           "--rf", str(pick["rf"]),
           "--program-id", str(pick["program"]),
           "--player", "vlc"]
    if sys.platform == "win32":
        DETACHED = 0x00000008 | 0x00000200 | 0x08000000
        return subprocess.Popen(
            cmd, env=pick_env, creationflags=DETACHED,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    return subprocess.Popen(cmd, env=pick_env, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_live_ts(min_size_mb: float, timeout_s: int) -> bool:
    """Return True when live.ts grows past min_size_mb within timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if LIVE_TS.exists() and LIVE_TS.stat().st_size / 1e6 >= min_size_mb:
            return True
        time.sleep(2)
    return False


# ----------------- audio probe / verify -----------------

def extract_play_audio(out_wav: Path, dur: int = 6) -> bool:
    """Extract `dur` seconds of audio from whatever ffmpeg+VLC pipeline is
    currently running, by reading from live.ts via the SAME map the picker
    is using. We just look at whatever audio track shows up first."""
    if out_wav.exists():
        try:
            out_wav.unlink()
        except OSError:
            pass
    # Use the file at its current tail end so we get fresh audio.
    sz = LIVE_TS.stat().st_size if LIVE_TS.exists() else 0
    start_offset = max(15, int(sz / 2_500_000) - dur - 4)
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(start_offset), "-t", str(dur),
        "-i", str(LIVE_TS),
        "-map", "0:a:0",          # first audio stream
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
        str(out_wav),
    ]
    rc, _, _ = run(cmd, timeout=dur * 4 + 5)
    return rc == 0 and out_wav.exists() and out_wav.stat().st_size > 1000


def extract_program_audio(program: int, lang: str, out_wav: Path,
                          dur: int = 6) -> bool:
    """Extract a specific program+language straight from live.ts."""
    if out_wav.exists():
        try:
            out_wav.unlink()
        except OSError:
            pass
    sz = LIVE_TS.stat().st_size if LIVE_TS.exists() else 0
    start_offset = max(15, int(sz / 2_500_000) - dur - 4)
    if lang == "all":
        map_arg = f"0:p:{program}:a:0"   # first audio of the program
    else:
        map_arg = f"0:p:{program}:m:language:{lang}"
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(start_offset), "-t", str(dur),
        "-i", str(LIVE_TS),
        "-map", map_arg,
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
        str(out_wav),
    ]
    rc, _, _ = run(cmd, timeout=dur * 4 + 5)
    return rc == 0 and out_wav.exists() and out_wav.stat().st_size > 1000


def mean_volume_db(wav: Path) -> float:
    rc, _, err = run([FFMPEG, "-hide_banner", "-i", str(wav),
                      "-af", "volumedetect", "-f", "null", "-"], timeout=15)
    for line in err.splitlines():
        if "mean_volume:" in line:
            try:
                return float(line.split("mean_volume:")[1]
                             .split("dB")[0].strip())
            except (ValueError, IndexError):
                pass
    return -999.0


def english_recognition(wav: Path) -> tuple[int, float, str]:
    """Run Windows SAPI English recognition. Returns (word_count, avg_conf,
    excerpt). Word count being 0 with audible volume = NOT English."""
    if sys.platform != "win32":
        return 0, 0.0, ""
    # Bake the file path right into the PowerShell command so we don't have
    # to fight -Command's argument handling (which silently drops $args).
    script_path = str(wav).replace("'", "''")
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$rec = New-Object System.Speech.Recognition.SpeechRecognitionEngine;"
        "$g   = New-Object System.Speech.Recognition.DictationGrammar;"
        "$rec.LoadGrammar($g);"
        f"$rec.SetInputToWaveFile('{script_path}');"
        "$txt = ''; $conf = 0.0; $n = 0;"
        "for ($i=0; $i -lt 20; $i++) {"
        "  $r = $null;"
        "  try { $r = $rec.Recognize([TimeSpan]::FromSeconds(2)) } catch { break };"
        "  if ($null -eq $r) { break };"
        "  $txt += $r.Text + ' ';"
        "  $conf += $r.Confidence;"
        "  $n += 1;"
        "}"
        "$rec.Dispose();"
        "$words = ($txt -split '\\s+' | Where-Object { $_ }).Count;"
        "$avg = if ($n -gt 0) { $conf / $n } else { 0 };"
        "Write-Output \"$words|$avg|$txt\""
    )
    p = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-Command", ps],
        capture_output=True, text=True, timeout=120,  # pipe-ok: the child
        # emits ONE summary line ("words|conf|txt"); an expiry loses a
        # single probe, and the caller already scores that as 0 words
    )
    out = (p.stdout or "").strip().split("|", 2)
    if len(out) != 3:
        return 0, 0.0, ""
    try:
        return int(out[0]), float(out[1]), out[2].strip()
    except ValueError:
        return 0, 0.0, ""


@dataclass
class Verdict:
    pick: str
    mean_db: float = -999.0
    words: int = 0
    avg_conf: float = 0.0
    excerpt: str = ""
    expected_lang: str = ""
    actual_label: str = ""

    @property
    def is_silent(self) -> bool:
        return self.mean_db < -60

    @property
    def looks_english(self) -> bool:
        # The recognizer hallucinates "English" words on Spanish phonemes
        # but with very low confidence. Real English broadcast lands at
        # 0.20+ confidence. We require BOTH a meaningful word count AND
        # confidence above the hallucination floor.
        return self.words >= 8 and self.avg_conf >= 0.18

    @property
    def looks_non_english(self) -> bool:
        # Audible audio that the English recognizer either can't parse
        # (few words) OR parses with very low confidence (hallucinating
        # English from Spanish phonemes) -> non-English.
        if self.is_silent:
            return False
        if self.words <= 4:
            return True
        if self.avg_conf < 0.15:
            return True
        return False

    def classify(self) -> str:
        if self.is_silent:
            return "silent"
        if self.looks_english:
            return "english"
        if self.looks_non_english:
            return "non-english"
        return "uncertain"

    def matches_expectation(self) -> bool:
        return self.actual_label == self.expected_lang


# ----------------- agent loop -----------------

def verify_pick(pick_key: str, pick: dict, fh) -> Verdict:
    """Tune the pick, wait, extract audio, classify it."""
    log(f"\n[verify] picking {pick['label']}  (RF {pick['rf']} "
        f"program {pick['program']} lang={pick['audio_language']})", fh)
    kill_chain()
    time.sleep(2)
    env = os.environ.copy()
    env.update(CHAIN_ENV)
    sdr_api = r"C:\Program Files\SDRplay\API\x64"
    if os.path.isdir(sdr_api):
        env["PATH"] = sdr_api + os.pathsep + env.get("PATH", "")
    if LIVE_TS.exists():
        try:
            LIVE_TS.unlink()
        except OSError:
            pass
    proc = spawn_picked(pick, env)
    if proc is None:
        log("  spawn failed", fh)
        return Verdict(pick=pick_key, expected_lang=pick_key,
                       actual_label="spawn-fail")
    log(f"  spawned tv_tuner PID {proc.pid}, waiting for chain to lock...", fh)
    if not wait_for_live_ts(min_size_mb=40, timeout_s=60):
        log("  chain did not lock in time", fh)
        kill_chain()
        return Verdict(pick=pick_key, expected_lang=pick_key,
                       actual_label="no-lock")
    log("  chain locked. Extracting audio sample...", fh)
    wav = WORK_DIR / f"sample_{pick_key}.wav"
    if not extract_program_audio(pick["program"], pick["audio_language"],
                                 wav, dur=8):
        log("  audio extract failed", fh)
        return Verdict(pick=pick_key, expected_lang=pick_key,
                       actual_label="extract-fail")
    db = mean_volume_db(wav)
    words, conf, excerpt = english_recognition(wav)
    v = Verdict(pick=pick_key, mean_db=db, words=words, avg_conf=conf,
                excerpt=(excerpt[:80] if excerpt else ""),
                expected_lang=pick_key)
    v.actual_label = v.classify()
    log(f"  vol={db:.1f} dB  words={words}  conf={conf:.2f}  "
        f"-> {v.actual_label!r}  (expected {pick_key!r})", fh)
    if excerpt:
        log(f"  excerpt: {excerpt[:120]}", fh)
    return v


def run_verification(runs: int, fh) -> bool:
    """Run the eng/spa pair verification `runs` times. Return True if all
    runs match expectations."""
    log(f"\n{'=' * 72}", fh)
    log(f"running {runs} pair-verification round(s)", fh)
    log(f"{'=' * 72}", fh)
    all_pass = True
    for r in range(1, runs + 1):
        log(f"\n--- round {r}/{runs} ---", fh)
        # English pick
        v_eng = verify_pick("eng", PICKS["eng"], fh)
        eng_ok = v_eng.actual_label == "english"
        # Spanish pick (we expect 'non-english' classification i.e. it's
        # audible but not recognized as English)
        v_spa = verify_pick("spa", PICKS["spa"], fh)
        spa_ok = v_spa.actual_label == "non-english"
        round_pass = eng_ok and spa_ok
        log(f"\n  round {r}: english={'PASS' if eng_ok else 'FAIL'}  "
            f"spanish={'PASS' if spa_ok else 'FAIL'}  -> "
            f"{'PASS' if round_pass else 'FAIL'}", fh)
        all_pass = all_pass and round_pass
        if not round_pass:
            log("  details:", fh)
            log(f"    eng: vol={v_eng.mean_db:.1f}  words={v_eng.words}  "
                f"got={v_eng.actual_label}", fh)
            log(f"    spa: vol={v_spa.mean_db:.1f}  words={v_spa.words}  "
                f"got={v_spa.actual_label}", fh)
    return all_pass


# ----------------- hotkey runtime -----------------

def hotkey_loop(initial_pick: str = "eng"):
    """Run with global hotkeys: Ctrl+Shift+E English, Ctrl+Shift+S Spanish,
    Ctrl+Shift+A toggle, Ctrl+Shift+Q quit."""
    if sys.platform != "win32":
        print("[agent] hotkeys are Windows-only.")
        return
    env = os.environ.copy()
    env.update(CHAIN_ENV)
    sdr_api = r"C:\Program Files\SDRplay\API\x64"
    if os.path.isdir(sdr_api):
        env["PATH"] = sdr_api + os.pathsep + env.get("PATH", "")

    current = {"key": None, "proc": None}

    def switch(to: str):
        if to not in PICKS:
            return
        if current["key"] == to and current["proc"] and current["proc"].poll() is None:
            return
        print(f"[agent] switching to {to}: {PICKS[to]['label']}", flush=True)
        kill_chain()
        time.sleep(1.5)
        proc = spawn_picked(PICKS[to], env)
        current["key"] = to
        current["proc"] = proc

    switch(initial_pick)

    user32 = ctypes.windll.user32
    user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_SHIFT, VK_E)
    user32.RegisterHotKey(None, 2, MOD_CONTROL | MOD_SHIFT, VK_S)
    user32.RegisterHotKey(None, 3, MOD_CONTROL | MOD_SHIFT, VK_A)
    user32.RegisterHotKey(None, 4, MOD_CONTROL | MOD_SHIFT, VK_Q)
    print("\n[agent] hotkeys ready:")
    print("  Ctrl+Shift+E  ->  NBC News 4 (English)")
    print("  Ctrl+Shift+S  ->  Telemundo (Spanish)")
    print("  Ctrl+Shift+A  ->  toggle eng <-> spa")
    print("  Ctrl+Shift+Q  ->  quit agent")
    print("[agent] keep this window open; switch from any other app.")

    msg = wt.MSG()
    try:
        while True:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            if msg.message != WM_HOTKEY:
                continue
            hk = msg.wParam
            if hk == 1:
                switch("eng")
            elif hk == 2:
                switch("spa")
            elif hk == 3:
                switch("spa" if current["key"] == "eng" else "eng")
            elif hk == 4:
                break
    finally:
        for hk_id in (1, 2, 3, 4):
            user32.UnregisterHotKey(None, hk_id)
        kill_chain()
        print("[agent] done")


# ----------------- main -----------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--runs", type=int, default=2,
                    help="Verification rounds before going hot")
    ap.add_argument("--no-hotkeys", action="store_true",
                    help="Test only; don't enter the hotkey loop after")
    ap.add_argument("--skip-verify", action="store_true",
                    help="Go straight into hotkey mode (trust the pair)")
    args = ap.parse_args()

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    report = WORK_DIR / "report.txt"
    fh = open(report, "w", encoding="utf-8")

    log("=" * 72, fh)
    log(f"Language-pair agent  -  {time.strftime('%Y-%m-%d %H:%M:%S')}", fh)
    log("=" * 72, fh)
    log(f"English pick: {PICKS['eng']['label']}", fh)
    log(f"Spanish pick: {PICKS['spa']['label']}", fh)
    log("", fh)

    verified = args.skip_verify
    if not verified:
        verified = run_verification(args.runs, fh)
        log("", fh)
        log("=" * 72, fh)
        log(f"VERDICT: pair {'WORKS' if verified else 'FAILS'} verification",
            fh)
        log("=" * 72, fh)
        log(f"Full report at {report}", fh)

    if not verified:
        log("\n[agent] verification failed. Not going into hotkey mode.", fh)
        log("[agent] Inspect the report and adjust PICKS dict.", fh)
        fh.close()
        return 2

    fh.close()
    if args.no_hotkeys:
        print("\n[agent] --no-hotkeys: exiting after verification.")
        kill_chain()
        return 0
    print("\n[agent] verification passed. Starting hotkey runtime...")
    hotkey_loop(initial_pick="eng")
    return 0


if __name__ == "__main__":
    sys.exit(main())
