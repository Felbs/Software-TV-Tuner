# Install on Windows

Roughly 10 minutes. The reference setup is an SDRplay RSPdx, but any
SoapySDR-supported SDR works.

## What you need

1. **GNU Radio 3.10+** — the easy way is [`radioconda`](https://github.com/ryanvolz/radioconda)
   (free, bundles GNU Radio + SoapySDR + Python).
2. **An SDR** — reference is an SDRplay RSPdx (install the free **SDRplay
   API v3** driver from [sdrplay.com](https://www.sdrplay.com/)). RTL-SDR,
   HackRF, Airspy, and BladeRF also work — see the SDR table in the main
   [README](../../README.md#what-sdrs-work).
3. **ffmpeg** — grab a [full build](https://www.gyan.dev/ffmpeg/builds/)
   and extract it to `C:\ffmpeg\`.
4. **Visual Studio 2022 Build Tools** (with the C++ workload) — needed
   once to compile the decoder module.
5. **Any antenna.** Amplified/directional TV antennas do best, but the
   software calibrates to whatever you have — that's the whole point.

## Steps

```powershell
# 1. Clone
git clone https://github.com/Felbs/Software-TV-Tuner.git
cd Software-TV-Tuner

# 2. Build the C++ decoder module (uses VS 2022 Build Tools + NMake)
gr-atscplus\_build.bat

# 3. Verify the blocks load
python -c "from gnuradio import atscplus; print(dir(atscplus))"

# 4. Player runtime deps
python -m pip install opencv-python sounddevice

# 5. Run it
python tools\tv_tuner.py
```

Run everything from a **radioconda** prompt so GNU Radio, SoapySDR, and
the SDRplay DLLs are all on the path.

## Notes

- Edited the `gr-atscplus` C++ and rebuilding? `_rebuild.bat` **compiles
  but does not install** — follow it with `cmake --install` (or copy the
  built module into place) or Python keeps importing the stale one.
- Stuck? See [Troubleshooting](../../README.md#troubleshooting) in the
  README (the `--probe` device check catches most SDR issues).

Next: [what to run once it's installed →](../../README.md#run)
