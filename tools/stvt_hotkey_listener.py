"""Global hotkey listener for STVT audio-language toggle.

Runs in the background and registers Win32 hotkeys that work no matter
which window has focus (VLC, browser, anywhere). Press a hotkey and the
running STVT chain swaps audio language in ~5 seconds.

Default hotkeys:
    Ctrl+Shift+E   ->  English
    Ctrl+Shift+S   ->  Spanish
    Ctrl+Shift+A   ->  toggle (cycle EN -> ES -> ALL)
    Ctrl+Shift+Q   ->  quit this listener

Run it once and leave it minimized:
    python tools/stvt_hotkey_listener.py

No external packages needed — uses the Win32 RegisterHotKey API
directly via ctypes. Windows-only.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
from pathlib import Path

# Win32 modifier flags
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

# Virtual-key codes
VK_A = 0x41
VK_E = 0x45
VK_S = 0x53
VK_Q = 0x51
VK_UP = 0x26
VK_DOWN = 0x28

REPO = Path(__file__).resolve().parents[1]
AUDIO_LANG_SCRIPT  = REPO / "tools" / "stvt_audio_lang.py"
CHAN_STEP_SCRIPT   = REPO / "tools" / "stvt_channel_step.py"
PYEXE = sys.executable

# Hotkey ID -> (label, action). audio_lang verbs go to stvt_audio_lang.py;
# channel-change verbs go to stvt_channel_step.py.
HOTKEYS = {
    1: ("Ctrl+Shift+E -> English",        "audio:eng"),
    2: ("Ctrl+Shift+S -> Spanish",        "audio:spa"),
    3: ("Ctrl+Shift+A -> toggle",         "audio:toggle"),
    4: ("Ctrl+Shift+Up -> next channel",  "chan:next"),
    5: ("Ctrl+Shift+Down -> prev channel","chan:prev"),
    6: ("Ctrl+Shift+Q -> quit",           None),
}


def fire(action: str):
    """Run stvt_audio_lang.py with the given action in the background."""
    if action is None:
        return
    print(f"  [hotkey] firing: {action}", flush=True)
    # Action grammar: "audio:<verb>" -> stvt_audio_lang.py <verb>
    #                 "chan:<verb>"  -> stvt_channel_step.py <verb>
    try:
        if ":" in action:
            kind, verb = action.split(":", 1)
            if kind == "audio":
                script, args = AUDIO_LANG_SCRIPT, [verb]
            elif kind == "chan":
                script, args = CHAN_STEP_SCRIPT, [verb]
            else:
                return
        else:
            # back-compat: bare verb defaults to audio
            script, args = AUDIO_LANG_SCRIPT, [action]
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        DETACHED = 0x00000008 | 0x00000200 | 0x08000000
        subprocess.Popen(
            [PYEXE, "-u", str(script), *args],
            creationflags=DETACHED,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"  [hotkey] failed: {e}", flush=True)


def main() -> int:
    if sys.platform != "win32":
        print("[hotkey] this listener is Windows-only", file=sys.stderr)
        return 1

    user32 = ctypes.windll.user32

    bindings = {
        "Ctrl+Shift+E":    (MOD_CONTROL | MOD_SHIFT, VK_E),
        "Ctrl+Shift+S":    (MOD_CONTROL | MOD_SHIFT, VK_S),
        "Ctrl+Shift+A":    (MOD_CONTROL | MOD_SHIFT, VK_A),
        "Ctrl+Shift+Q":    (MOD_CONTROL | MOD_SHIFT, VK_Q),
        "Ctrl+Shift+Up":   (MOD_CONTROL | MOD_SHIFT, VK_UP),
        "Ctrl+Shift+Down": (MOD_CONTROL | MOD_SHIFT, VK_DOWN),
    }
    registered = []
    for hk_id, (label, _) in HOTKEYS.items():
        chord = label.split(" ->")[0].strip()
        if chord not in bindings:
            continue
        mod, vk = bindings[chord]
        if user32.RegisterHotKey(None, hk_id, mod, vk):
            registered.append(hk_id)
            print(f"  registered  {label}")
        else:
            err = ctypes.get_last_error()
            print(f"  FAILED to register {label} (Windows error {err})")

    if not registered:
        print("[hotkey] no hotkeys registered — exiting")
        return 1

    print()
    print("[hotkey] listening. Minimize this window and use the hotkeys")
    print("[hotkey] from anywhere. Press Ctrl+Shift+Q to quit cleanly.")
    print()

    # GetMessage loop
    msg = wt.MSG()
    try:
        while True:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0 or r == -1:
                break
            if msg.message == WM_HOTKEY:
                hk_id = msg.wParam
                meta = HOTKEYS.get(hk_id)
                if not meta:
                    continue
                label, action = meta
                print(f"[hotkey] {label}")
                if action is None:
                    break  # quit
                fire(action)
    except KeyboardInterrupt:
        pass
    finally:
        for hk_id in registered:
            user32.UnregisterHotKey(None, hk_id)
        print("[hotkey] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
