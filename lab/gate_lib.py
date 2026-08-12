"""gate_lib.py — THE VALID GATE. Multi-run, modal-hash, frames-with-spread.

────────────────────────────────────────────────────────────────────────────
THE LAW (measured 2026-07-29, re-confirmed 2026-07-29 evening)

  NO decode path in this tree is bit-reproducible across processes.

  `atsc_equalizer_long`: 2 distinct md5s in 6 identical rf34 runs, 3 in 3
  identical rf7 runs. `atsc_equalizer_wl` (both fused and legacy): 3 distinct
  md5s in 15 identical runs (`af9769a6` x12, `d8b4f370` x2, `55eb2faa` x1),
  and the pre-merge build gave a different third hash again.

  CAUSE: volk selects its dot-product kernel at CALL TIME from the runtime
  ALIGNMENT of the pointers it is handed. The number of items a GR work call
  receives depends on thread scheduling, so the same sample lands at a
  different offset in the buffer from run to run, a different kernel runs, the
  summation order changes, and the result differs in the last ~1e-4. In a
  feedback loop (LMS taps, timing loop) that divergence COMPOUNDS — from field
  ~26 onward the two runs are genuinely different decodes.

  CONSEQUENCE: **a single-run md5 comparison is not a gate.** It is a coin
  flip weighted by whichever hash happens to be modal. Any claim of the form
  "the hash matched, therefore the path is unchanged" made from ONE run is
  void — and so is any +/-2-frame claim from one run.

  THE VALID TEST: run N >= 3 (default 5) times and gate on
      * the MODAL hash being drawn from the known/expected hash SET, OR
      * the MEDIAN frame count being within tolerance of the expected one,
  while ALWAYS printing the full hash set and the frame median/spread so the
  reader can see the noise they are being asked to accept.
────────────────────────────────────────────────────────────────────────────

Usage (the common case — is the default path still the default path?):

    import sys; sys.path.insert(0, r"Z:\\src\\magic-tv-decoder\\lab")
    from gate_lib import replay_multi, gate, render

    rows = replay_multi(IQ, tag="g1_default", env={"STVT_EQ": "long"}, runs=5)
    res  = gate(rows, expect_md5=KNOWN_RF34_LONG, expect_frames=403,
                name="G1 default path")
    print(render(res))
    sys.exit(0 if res.passed else 1)

`KNOWN_*` sets of already-observed hashes live at the bottom of this file so
future sessions add to them instead of re-discovering the wobble.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Iterable, Sequence

REPO = Path(__file__).resolve().parents[1]
PY = os.environ.get("STVT_PY", os.path.join(os.environ.get("USERPROFILE", ""), "radioconda", "python.exe"))

#: the minimum run count that makes a hash claim meaningful (see THE LAW)
MIN_RUNS = 3
#: default run count
DEFAULT_RUNS = 5

RE_FRAME = re.compile(r"frame=\s*(\d+)")


def _in_known(h: str, known: Iterable[str]) -> bool:
    """Known-set membership with PREFIX support.

    Several historical hashes were only ever written down as 8-char prefixes in
    the worklogs. A known-set entry shorter than 32 chars is treated as a
    prefix so those records stay usable without inventing the missing digits.
    """
    if not h:
        return False
    h = h.upper()
    for k in known:
        k = k.upper()
        if len(k) >= 32:
            if h == k:
                return True
        elif len(k) >= 8 and h.startswith(k):
            return True
    return False


class SingleRunGateError(RuntimeError):
    """Raised when a gate is asked to judge fewer than MIN_RUNS runs.

    This exists so the invalid single-run md5 test cannot be used by accident.
    If you really only have one run (e.g. a 4-hour capture), pass
    allow_single_run="<why>" and the reason is printed with the verdict.
    """


# ── primitives ───────────────────────────────────────────────────────────────

def md5(p: str | Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def frames(ts: str | Path, min_bytes: int = 2_000_000) -> int:
    """Delivered VIDEO frames — ffmpeg null-sink `-map 0:v`.

    The ONLY trustworthy quality gauge here: ffprobe lies on multi-program TS
    and `-v error` suppresses the -stats line that carries the answer.
    """
    ts = Path(ts)
    if not ts.exists() or ts.stat().st_size < min_bytes:
        return 0
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
         "-err_detect", "ignore_err", "-analyzeduration", "100M",
         "-probesize", "100M", "-i", str(ts), "-map", "0:v", "-f", "null", "-"],
        capture_output=True, text=True)
    m = RE_FRAME.findall(r.stderr)
    return int(m[-1]) if m else 0


# ── one run ──────────────────────────────────────────────────────────────────

@dataclass
class RunRow:
    tag: str
    run: int
    md5: str = ""
    frames: int = 0
    wall_s: float = 0.0
    ts_bytes: int = 0
    ts: str = ""
    log: str = ""
    extra: dict = field(default_factory=dict)


#: TEST-ONLY determinism knob — never a default, it costs 1.6-1.8x wall time.
#: volk 3.2.0 honours VOLK_GENERIC=1, which forces the plain-C implementation of
#: every kernel, so the dot-product summation order no longer depends on the
#: runtime pointer alignment. MEASURED 2026-07-29 on rf34_ctrl:
#:    STVT_EQ=long  3/3 identical -> F1F867C5567B33721684F4FBF7C423BB (27.8 s/run
#:                  vs 15.5 s SIMD) — and that is the DOCUMENTED MODAL HASH
#:    STVT_EQ=wl    3/3 identical -> D8B4F370... (25.3 s vs 15.7 s), a member of
#:                  the recorded WL wobble set
#: So a bit-exact single-run comparison IS available for tests, at a ~1.7x cost.
#: It does NOT replace the modal gate: production runs SIMD, and a change that
#: only shows up in the SIMD summation order would slip past a generic-only
#: test. Use it to bisect a suspected real difference, not as the gate.
DETERMINISTIC_ENV = {"VOLK_GENERIC": "1"}
DETERMINISTIC_REF = {
    ("rf34_ctrl", "long"): "F1F867C5567B33721684F4FBF7C423BB",
    ("rf34_ctrl", "wl"): "D8B4F370",
}


def replay_once(iq: str | Path, tag: str, i: int, env: dict,
                outdir: str | Path, *, tool: str = "tv_replay.py",
                args: Sequence[str] = (), keep: bool = False,
                count_frames: bool = True,
                deterministic: bool = False,
                parse: Callable[[str], dict] | None = None) -> RunRow:
    """One `tools/<tool>` replay -> a RunRow. Deletes the TS unless keep.

    deterministic=True adds DETERMINISTIC_ENV (VOLK_GENERIC=1) — test-only, see
    that constant's note.
    """
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    ts = outdir / f"{tag}_{i:03d}.ts"
    log = outdir / f"{tag}_{i:03d}.log"
    e = dict(os.environ)
    e.update({k: str(v) for k, v in env.items()})
    if deterministic:
        e.update(DETERMINISTIC_ENV)
    t0 = time.time()
    subprocess.run([PY, str(REPO / "tools" / tool), "--iq", str(iq),
                    "--out", str(ts), "--log", str(log), *args],
                   env=e, cwd=str(REPO), capture_output=True, text=True)
    row = RunRow(tag=tag, run=i, wall_s=round(time.time() - t0, 1),
                 ts=str(ts), log=str(log))
    if ts.exists():
        row.ts_bytes = ts.stat().st_size
        row.md5 = md5(ts)
        if count_frames:
            row.frames = frames(ts)
    if parse and log.exists():
        row.extra = parse(log.read_text(errors="replace"))
    if not keep and ts.exists():
        ts.unlink()
    return row


def replay_multi(iq: str | Path, tag: str, env: dict, *,
                 runs: int = DEFAULT_RUNS, outdir: str | Path | None = None,
                 tool: str = "tv_replay.py", args: Sequence[str] = (),
                 keep: bool = False, count_frames: bool = True,
                 deterministic: bool = False,
                 parse: Callable[[str], dict] | None = None,
                 progress: bool = True) -> list[RunRow]:
    """N identical replays. N defaults to DEFAULT_RUNS because of THE LAW."""
    outdir = Path(outdir or (REPO / "lab" / "gate_runs" / tag))
    rows: list[RunRow] = []
    for i in range(1, runs + 1):
        r = replay_once(iq, tag, i, env, outdir, tool=tool, args=args,
                        keep=keep, count_frames=count_frames,
                        deterministic=deterministic, parse=parse)
        rows.append(r)
        if progress:
            print(f"  [{tag} {i}/{runs}] md5={r.md5[:8]} frames={r.frames} "
                  f"bytes={r.ts_bytes} ({r.wall_s}s)", flush=True)
    return rows


# ── statistics over runs ─────────────────────────────────────────────────────

@dataclass
class HashStats:
    n: int
    modal: str
    modal_n: int
    counts: dict           # md5 -> count, most common first
    reproducible: bool     # True only if ONE hash in N runs

    def __str__(self) -> str:
        parts = ", ".join(f"{h[:8]}x{c}" for h, c in self.counts.items())
        tail = "BIT-REPRODUCIBLE" if self.reproducible else \
               f"{len(self.counts)} distinct hashes in {self.n} runs"
        return f"modal={self.modal[:8]} ({self.modal_n}/{self.n})  [{parts}]  {tail}"


@dataclass
class FrameStats:
    n: int
    median: float
    lo: int
    hi: int
    values: list

    @property
    def spread(self) -> int:
        return self.hi - self.lo

    def __str__(self) -> str:
        return (f"frames median={self.median:g} range={self.lo}-{self.hi} "
                f"(spread {self.spread}) values={self.values}")


def hash_stats(rows: Iterable[RunRow]) -> HashStats:
    hs = [r.md5 for r in rows if r.md5]
    c = Counter(hs)
    modal, modal_n = (c.most_common(1)[0] if c else ("", 0))
    return HashStats(n=len(hs), modal=modal, modal_n=modal_n,
                     counts=dict(c.most_common()),
                     reproducible=(len(c) == 1 and len(hs) >= MIN_RUNS))


def frame_stats(rows: Iterable[RunRow]) -> FrameStats:
    v = [r.frames for r in rows]
    return FrameStats(n=len(v), median=statistics.median(v) if v else 0,
                      lo=min(v) if v else 0, hi=max(v) if v else 0, values=v)


# ── the gate ─────────────────────────────────────────────────────────────────

@dataclass
class GateResult:
    name: str
    passed: bool
    reasons: list           # every criterion evaluated, in order
    hashes: HashStats
    frames: FrameStats
    rows: list
    note: str = ""

    def to_json(self) -> str:
        d = asdict(self)
        d["hashes"] = asdict(self.hashes)
        d["frames"] = asdict(self.frames)
        d["rows"] = [asdict(r) for r in self.rows]
        return json.dumps(d, indent=1)


def gate(rows: Sequence[RunRow], *, name: str = "gate",
         expect_md5: str | Iterable[str] | None = None,
         expect_frames: int | None = None,
         frame_tol: int = 2,
         require_self_consistent: bool = True,
         allow_single_run: str | None = None) -> GateResult:
    """Judge N runs. PASSES on modal-hash-in-the-known-set OR frames-in-tolerance.

    expect_md5    a hash, or the SET of hashes this path has ever legitimately
                  produced. The gate checks the MODAL hash is in that set —
                  never that every run matched.
    expect_frames the recorded frame count. Compared against the MEDIAN.
    frame_tol     +/- band on the median (and, when there is no expectation to
                  compare against, the maximum acceptable run-to-run spread).

    Raises SingleRunGateError for fewer than MIN_RUNS runs unless
    allow_single_run="<reason>" is supplied — that is the whole point of this
    module (see THE LAW).
    """
    if len(rows) < MIN_RUNS and not allow_single_run:
        raise SingleRunGateError(
            f"{name}: {len(rows)} run(s) cannot gate a hash or a +/-{frame_tol} "
            f"frame claim — no decode path here is bit-reproducible across "
            f"processes (see gate_lib THE LAW). Use runs>={MIN_RUNS}, or pass "
            f"allow_single_run='<why this is acceptable>'.")

    hs = hash_stats(rows)
    fs = frame_stats(rows)
    reasons: list[str] = []
    verdicts: list[bool] = []

    if expect_md5 is not None:
        known = {expect_md5.upper()} if isinstance(expect_md5, str) else \
                {h.upper() for h in expect_md5}
        ok = _in_known(hs.modal, known)
        verdicts.append(ok)
        reasons.append(
            f"{'PASS' if ok else 'FAIL'} modal hash {hs.modal[:8]} "
            f"{'in' if ok else 'NOT in'} known set "
            f"{{{', '.join(sorted(h[:8] for h in known))}}}")
        unseen = [h for h in hs.counts if not _in_known(h, known)]
        if unseen:
            reasons.append(
                f"note new hash(es) observed (add to the known set if the "
                f"frames gate passes): {', '.join(sorted(h[:8] for h in unseen))}")

    if expect_frames is not None:
        ok = abs(fs.median - expect_frames) <= frame_tol
        verdicts.append(ok)
        reasons.append(
            f"{'PASS' if ok else 'FAIL'} median frames {fs.median:g} vs "
            f"expected {expect_frames} (tol +/-{frame_tol})")

    if not verdicts:
        # characterisation run: nothing to compare against, so the only
        # question we can honestly answer is whether the runs agree with
        # EACH OTHER.
        ok = (not require_self_consistent) or fs.spread <= frame_tol
        verdicts.append(ok)
        reasons.append(
            f"{'PASS' if ok else 'FAIL'} no expectation given; frame spread "
            f"{fs.spread} <= tol {frame_tol} (self-consistency only)")

    # modal-match OR frames-in-tolerance: the OR is deliberate. A new hash with
    # the right frame count is the volk wobble; a matching hash with the wrong
    # frame count cannot happen (same bytes => same frames).
    passed = any(verdicts)
    note = allow_single_run or ""
    if allow_single_run:
        reasons.append(f"note SINGLE-RUN EXEMPTION CLAIMED: {allow_single_run}")
    return GateResult(name=name, passed=passed, reasons=reasons, hashes=hs,
                      frames=fs, rows=list(rows), note=note)


def render(res: GateResult) -> str:
    """The printable evidence block. Always shows the whole hash set."""
    L = [f"=== {res.name}: {'PASS' if res.passed else 'FAIL'} "
         f"({res.hashes.n} runs) ===",
         f"  hashes: {res.hashes}",
         f"  {res.frames}"]
    L += [f"  {r}" for r in res.reasons]
    return "\n".join(L)


def control_ok(rows_a: Sequence[RunRow], rows_b: Sequence[RunRow], *,
               frame_tol: int = 2) -> tuple[bool, str]:
    """Two run-sets that MUST be the same decode (e.g. tv_dual's `long` leg
    across arms, or a before/after default-path check).

    The valid comparison is: modal hashes agree OR the hash sets overlap OR the
    frame medians are within tolerance. NOT `a.md5 == b.md5` on one run each.
    """
    ha, hb = hash_stats(rows_a), hash_stats(rows_b)
    fa, fb = frame_stats(rows_a), frame_stats(rows_b)
    if ha.modal and ha.modal == hb.modal:
        return True, f"modal hashes agree ({ha.modal[:8]})"
    overlap = set(ha.counts) & set(hb.counts)
    if overlap:
        return True, (f"hash sets overlap ({', '.join(sorted(h[:8] for h in overlap))})"
                      f" — modal differs ({ha.modal[:8]} vs {hb.modal[:8]}), which is"
                      f" the volk wobble, not a change")
    if abs(fa.median - fb.median) <= frame_tol:
        return True, (f"disjoint hash sets but frame medians agree "
                      f"({fa.median:g} vs {fb.median:g}, tol {frame_tol})")
    return False, (f"hash sets disjoint AND frame medians differ "
                   f"({fa.median:g} vs {fb.median:g}): {ha} | {hb}")


# ── the known-hash registry (grow it, never shrink it) ───────────────────────
# Every entry below was OBSERVED on this tree from an unchanged code path, so a
# future run producing one of them is not mistaken for a regression. Entries
# shorter than 32 chars are 8-char PREFIXES — that is all the worklogs recorded
# for some of them, and _in_known() matches on prefix rather than inventing the
# missing digits.

KNOWN = {
    # tv_replay STVT_EQ=long, lab/marginal_iq/rf34_ctrl.cs16, arsenal env
    # (speed_build/WORKLOG §1 + §11.4 G1)
    ("rf34_ctrl", "long"): {
        "F1F867C5567B33721684F4FBF7C423BB",   # modal
        "AA0DB81B", "3D8C11EE",
    },
    # tv_replay STVT_EQ=wl (fused), same capture/env (§11.5b: 3 in 15 runs)
    ("rf34_ctrl", "wl"): {
        "AF9769A6F60C2BEBF6C6A50CF7CD8440",   # modal
        "55EB2FAA", "D8B4F370", "BF5FFB10",
    },
    # tv_replay STVT_EQ=long, rf7_marg — never reproducible (3 in 3 runs)
    ("rf7_marg", "long"): {"AC5FF168", "2B3D7075"},
}

KNOWN_RF34_LONG = KNOWN[("rf34_ctrl", "long")]
KNOWN_RF34_WL = KNOWN[("rf34_ctrl", "wl")]

#: recorded FRAME counts — the real gate (see speed_build/WORKLOG §11.4)
EXPECT_FRAMES = {
    ("rf34_ctrl", "long"): 403,
    ("rf34_ctrl", "wl"): 403,
    ("rf7_marg", "long"): 251,      # 250-251 band
    ("rf7_marg", "wl"): 257,
    ("rf9_marg", "long"): 112,
    ("rf9_marg", "wl"): 349,        # 348-350 band
    ("rf34_knee", "long"): 130,     # +AWGN 2147 seed 42
    ("rf34_knee", "wl"): 226,       # 226-230 band
}
