#!/usr/bin/env bash
# Deterministic equalizer A/B sweep on a captured IQ file.
# Replays the SAME bytes through each tuning combo and reports the
# fraction of valid (non-null) TS packets decoded. Higher = better.
set -u
IQ="${1:-$HOME/iq_rf36_outside.cs16}"
REPO="$HOME/Software-TV-Tuner"
OUTDIR=/tmp/eq_sweep
mkdir -p "$OUTDIR"
cd "$REPO" || exit 1

# Production base every config inherits.
BASE="STVT_RS=stock STVT_VITERBI=hard STVT_EQ=long STVT_SPS=1.1 STVT_RRC_SYMS=4 \
STVT_FPLL_FOLD=1 STVT_FPLL_BLOCK_NCO=1"

# name|extra-env  (extra overrides/augments BASE)
CONFIGS=(
  "baseline|"
  "gear|STVT_EQ_GEAR_LMS=1 STVT_EQ_LKG=1"
  "slowbeta|STVT_EQ_BETA=2e-5"
  "fsavg4|STVT_EQ_FS_AVG_DEPTH=4"
  "gear+slowbeta|STVT_EQ_GEAR_LMS=1 STVT_EQ_LKG=1 STVT_EQ_BETA=2e-5"
  "gear+fsavg|STVT_EQ_GEAR_LMS=1 STVT_EQ_LKG=1 STVT_EQ_FS_AVG_DEPTH=4"
  "rls|STVT_EQ_RLS=1"
  "erasure|STVT_RS=erasure"
)

# Counts valid vs null TS packets in a .ts file.
count_good() {
python3 - "$1" <<'PY'
import sys
p=sys.argv[1]
good=null=bad=0
try:
    with open(p,'rb') as f:
        data=f.read()
except FileNotFoundError:
    print("0 0 0"); sys.exit()
n=len(data)//188
for i in range(n):
    pkt=data[i*188:i*188+188]
    if pkt[0]!=0x47:
        bad+=1; continue
    pid=((pkt[1]&0x1f)<<8)|pkt[2]
    if pid==0x1fff: null+=1
    else: good+=1
tot=good+null+bad
print(f"{good} {null} {bad}")
PY
}

echo "IQ: $IQ ($(du -h "$IQ" 2>/dev/null | cut -f1))"
printf "%-16s %12s %12s %8s\n" "config" "good_pkts" "null_pkts" "good%"
echo "---------------------------------------------------------------"
for entry in "${CONFIGS[@]}"; do
  name="${entry%%|*}"; extra="${entry#*|}"
  out="$OUTDIR/$name.ts"; log="$OUTDIR/$name.log"
  rm -f "$out"
  env $BASE $extra python3 tools/tv_replay.py --iq "$IQ" --out "$out" --log "$log" \
      >/dev/null 2>&1
  read good null bad < <(count_good "$out")
  tot=$((good+null+bad))
  pct=$(awk -v g="$good" -v t="$tot" 'BEGIN{printf (t>0)?"%.1f":"0", 100.0*g/t}')
  printf "%-16s %12s %12s %7s%%\n" "$name" "$good" "$null" "$pct"
done
echo "---------------------------------------------------------------"
echo "Higher good% = more real data decoded from the SAME captured bytes."
