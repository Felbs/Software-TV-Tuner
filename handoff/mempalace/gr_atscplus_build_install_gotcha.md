---
name: gr-atscplus-build-install-gotcha
description: "`_rebuild.bat` only builds — it does NOT install. After any .cc change you must also run cmake --install + xcopy the python bindings, or radioconda keeps importing the stale .pyd."
metadata: 
  node_type: memory
  type: project
  originSessionId: 368b030c-6ab2-4abc-a2a8-33ccdf8dc873
---

`Z:\src\magic-tv-decoder\gr-atscplus\` has two build scripts:

- `_build.bat` — full clean wipe of `build/`, configure, build, **install**, xcopy bindings. Use for first build / after CMakeLists changes.
- `_rebuild.bat` — incremental `cmake --build .` only. **No install step.** This is the trap.

After a `.cc` source change, running `_rebuild.bat` produces a fresh `gnuradio-atscplus.dll` and `atscplus_python.cp312-win_amd64.pyd` inside `build/`, but `radioconda\Lib\site-packages\gnuradio\atscplus\atscplus_python.cp312-win_amd64.pyd` still points at the OLD pyd. Python imports the installed one, so your change has no effect — and there is no error to clue you in.

**Verification command** — confirm Python is loading the fresh pyd, not a stale one:
```powershell
$py = "$env:USERPROFILE\radioconda\python.exe"
& $py -c "import os; p = r'C:\Users\emane\radioconda\Lib\site-packages\gnuradio\atscplus\atscplus_python.cp312-win_amd64.pyd'; print('mtime:', os.path.getmtime(p))"
```
Compare mtime to your edit timestamp.

**Full deploy after a `_rebuild.bat`** (need vcvars + radioconda activated):
```batch
cmake --install . --config Release
xcopy /Y /I /Q "%USERPROFILE%\radioconda\Library\Lib\site-packages\gnuradio\atscplus\*" "%USERPROFILE%\radioconda\Lib\site-packages\gnuradio\atscplus\"
```
The xcopy step is necessary because cmake installs Python bindings to `radioconda\Library\Lib\site-packages\...` but Python actually imports from `radioconda\Lib\site-packages\...`. The `_build.bat` script does this mirror; `_rebuild.bat` skips it.

**Why this matters:** I lost ~15 minutes on the Windows-port A/B test before noticing the installed pyd was 3 days old. The rebuild "succeeded" and the Python `import` didn't error, but the new fs_checker code wasn't actually being exercised. Confirm install or you're testing nothing.

**TL;DR pattern:** for any future .cc change on Windows, prefer running `_build.bat` (slow but correct) over `_rebuild.bat`. If you do use `_rebuild.bat`, manually run `cmake --install` + the xcopy after.

Related: [[project-topology]] (Z:\src\magic-tv-decoder is the real repo).
