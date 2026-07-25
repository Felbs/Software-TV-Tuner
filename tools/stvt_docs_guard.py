#!/usr/bin/env python3
"""stvt_docs_guard.py - keep STVT's charts and telemetry contract honest.

Three checks (exit 1 on any error, so CI can gate on it):
 1. MERMAID LINT on ARCHITECTURE.md: unbalanced quotes/brackets, encoding rot,
    '-- >' arrow typos, unclosed fences ('%%' comments and id>"..."] flag nodes
    are valid mermaid). Style nits (raw specials in quoted labels) = warnings.
 2. ENDPOINT DRIFT: every "/api/x" the panel serves must be mentioned somewhere
    in the docs (ARCHITECTURE.md) - an uncharted endpoint is how contracts rot.
 3. TELEMETRY CONTRACT: the exact stderr tag strings the panel/lab parse
    (fs_err_rms=, [rs_erasure], [rs_turbo], viterbi_metric) must still be
    emitted by the chain source (gr-atscplus lib / tools) - renaming an emitter
    silently darkens the dials, which is the #1 telemetry breakage mode.
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ARCH = ROOT / "ARCHITECTURE.md"
PANEL = ROOT / "adaptive-tv" / "tv_tuna_panel.py"
LIBS = list((ROOT / "gr-atscplus" / "lib").glob("*.cc")) + [ROOT / "tools" / "tv_live.py"]

FENCE = re.compile(r"^```mermaid\s*$")
END = re.compile(r"^```\s*$")
ENTITY = re.compile(r"&[a-zA-Z]+[0-9]*;|&#\d+;")
ARROW_TYPO = re.compile(r"--\s+>")
FLAG_NODE = re.compile(r"\w+>\s*\"")
ALLOWED_TAGS = re.compile(r"</?(?:br|b|i|u|sub|sup)\s*/?>", re.I)
API_RE = re.compile(r'"(/api/[a-z_0-9]+)"')

# the telemetry tag strings that are load-bearing (parsed by panel/lab tools)
CONTRACT_TAGS = ["fs_err_rms=", "[rs_erasure", "[rs_turbo", "viterbi_metric"]


def lint(md: Path):
    errs, warns = [], []
    lines = md.read_text(encoding="utf-8").splitlines()
    in_b = False
    nb = 0
    for i, raw in enumerate(lines, 1):
        if not in_b and FENCE.match(raw):
            in_b = True; nb += 1; continue
        if in_b and END.match(raw):
            in_b = False; continue
        if not in_b:
            continue
        ln = raw.strip()
        if ln.startswith("%%"):
            continue
        if ARROW_TYPO.search(ln):
            errs.append(f"{md.name}:{i}: arrow typo '-- >'")
        if "�" in raw:
            errs.append(f"{md.name}:{i}: U+FFFD replacement char (encoding rot)")
        depth = -len(FLAG_NODE.findall(ln))
        in_q = False
        qbuf = []
        for ch in ln:
            if ch == '"':
                if in_q:
                    label = ALLOWED_TAGS.sub("", "".join(qbuf))
                    label = ENTITY.sub("", label)
                    if any(b in label for b in "<>&"):
                        warns.append(f"{md.name}:{i}: raw special in label (escape is safer)")
                    qbuf = []
                in_q = not in_q
            elif in_q:
                qbuf.append(ch)
            elif ch in "[({":
                depth += 1
            elif ch in "])}":
                depth -= 1
        if in_q:
            errs.append(f"{md.name}:{i}: unbalanced quote")
        if depth > 0:
            errs.append(f"{md.name}:{i}: unbalanced brackets (+{depth})")
    if in_b:
        errs.append(f"{md.name}: unclosed ```mermaid fence")
    return nb, errs, warns


def main():
    errs, warns = [], []
    nb, e, w = lint(ARCH)
    errs += e; warns += w
    print(f"[guard] {ARCH.name}: {nb} chart(s), {len(e)} error(s), {len(w)} warning(s)")
    # endpoint drift
    doc = ARCH.read_text(encoding="utf-8")
    served = sorted(set(API_RE.findall(PANEL.read_text(encoding="utf-8", errors="replace"))))
    missing = [ep for ep in served if ep not in doc]
    print(f"[guard] endpoints: {len(served)} served, {len(missing)} undocumented")
    for ep in missing:
        warns.append(f"DRIFT: panel serves {ep} but ARCHITECTURE.md never mentions it")
    # telemetry contract: parsed tags must still be emitted by the chain
    chain_src = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in LIBS if p.exists())
    for tag in CONTRACT_TAGS:
        if tag not in chain_src:
            errs.append(f"TELEMETRY BREAK: '{tag}' is parsed by panel/lab but NO "
                        f"chain source emits it - a dial just went dark")
    print(f"[guard] telemetry contract: {len(CONTRACT_TAGS)} load-bearing tags checked")
    for x in errs:
        print("  !!", x)
    for x in warns[:10]:
        print("  ~ ", x)
    if len(warns) > 10:
        print(f"  ~  ... +{len(warns)-10} more warnings")
    print(f"[guard] {'CLEAN' if not errs else 'FAIL'} (errors gate, warnings advise)")
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
