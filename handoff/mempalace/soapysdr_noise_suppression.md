---
name: soapysdr-noise-suppression
description: "Suppress SoapySDR's \"every optional driver failed to load\" cascade with `SoapySDR_setLogLevel(SOAPY_SDR_FATAL)` BEFORE the first device call."
metadata: 
  node_type: memory
  type: project
  originSessionId: 368b030c-6ab2-4abc-a2a8-33ccdf8dc873
---

Any tool that uses SoapySDR (directly or via sdr_sweep) spams stderr with:

```
[ERROR] SoapySDR::loadModule(airspyhfSupport.dll): LoadLibrary failed
[ERROR] SoapySDR::loadModule(audioSupport.dll): LoadLibrary failed
[ERROR] SoapySDR::loadModule(bladeRFSupport.dll): LoadLibrary failed
... 10 more for every uninstalled driver ...
[INFO] devIdx: 0
[INFO] SerNo: ...
[WARNING] SoapyVOLKConverters: no VOLK config file found
```

None of this is actionable when the user just wants their actual tool output. Suppress with:

```python
import SoapySDR
SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
```

Must run BEFORE the first `Device.make()` or `Device.enumerate()` call. In stvt_signal.py the pattern is: import SoapySDR + setLogLevel FIRST, then `import sdr_sweep` (which is what triggers the actual device enum).

**Why:** The user explicitly noticed the noise and asked for it gone. SoapySDR has no env var or default to skip enumeration of optional drivers; the only fix is to silence its log channel.

**Caveat:** Some C-level noise (e.g. from libvolk) may still print to raw stderr. If `setLogLevel(FATAL)` isn't enough, the next layer is redirecting stderr at the C level during the Soapy calls. Wasn't needed in practice for stvt_signal.py — `setLogLevel(FATAL)` was sufficient.

Apply this to any new tool that uses SoapySDR. See stvt_signal.py for the canonical 3-line snippet.
