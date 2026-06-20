#!/usr/bin/env bash
# Offline tuner-optimizer: search real-time-capable equalizer configs against
# a captured IQ file. Scores BOTH decode quality (good-packet %) AND wall-time
# (real-time feasibility). A config is only usable LIVE if it decodes the 30s
# capture in well under 30s of wall-clock.
set -u
IQ="${1:-$HOME/iq_rf36_outside.cs16}"
CAP_SECONDS="${2:-30}"
REPO="$HOME/Software-TV-Tuner"
OUTDIR=/tmp/eq_opt
mkdir -p "$OUTDIR"
cd "$REPO" || exit 1

BASE="STVT_RS=stock STVT_VITERBI=hard STVT_EQ=long STVT_SPS=1.1 STVT_RRC_SYMS=4 \
STVT_FPLL_FOLD=1 STVT_FPLL_BLOCK_NCO=1"

# Real-time-capable LMS-family candidates (RLS deliberately excluded — proven
# too slow live). name|extra-env
CONFIGS=(
  "baseline|"
  "beta_2e-5|STVT_EQ_BETA=2e-5"
  "beta_1e-5|STVT_EQ_BETA=1e-5"
  "beta_3e-5|STVT_EQ_BETA=3e-5"
  "dd_track|STVT_EQ_DD_MU=1e-3 STVT_EQ_DD_GATE=1"
  "beta2e5_dd|STVT_EQ_BETA=2e-5 STVT_EQ_DD_MU=1e-3 STVT_EQ_DD_GATE=1"
  "conv20|STVT_CONVERGENCE_SEC=20"
  "leak_low|STVT_EQ_LEAK=1e-3"
  "erasure|STVT_RS=erasure"
  "erasure_dd|STVT_RS=erasure STVT_EQ_DD_MU=1e-3 STVT_EQ_DD_GATE=1"
)

count_good() {
python3 - "$1" <<'PY'
import sys
p=sys.argv[1]; good=null=bad=0
try: data=open(p,'rb').read()
except FileNotFoundError: print("0 0 0"); sys.exit()
for i in range(len(data)//188):
    pkt=data[i*188:i*188+188]
    if pkt[0]!=0x47: bad+=1; continue
    pid=((pkt[1]&0x1f)<<8)|pkt[2]
    null += pid==0x1fff; good += pid!=0x1fff
print(f"{good} {null} {bad}")
PY
}

echo "IQ: $IQ   capture=${CAP_SECONDS}s"
printf "%-14s %8s %9s %12s %s\n" "config" "good%" "wall(s)" "realtime?" "verdict"
echo "----------------------------------------------------------------------"
base_pct=""
for entry in "${CONFIGS[@]}"; do
  name="${entry%%|*}"; extra="${entry#*|}"
  out="$OUTDIR/$name.ts"; rm -f "$out"
  t0=$(date +%s.%N)
  env $BASE $extra python3 tools/tv_replay.py --iq "$IQ" --out "$out" \
      --log "$OUTDIR/$name.log" >/dev/null 2>&1
  t1=$(date +%s.%N)
  wall=$(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.1f", b-a}')
  read good null bad < <(count_good "$out")
  tot=$((good+null+bad))
  pct=$(awk -v g="$good" -v t="$tot" 'BEGIN{printf (t>0)?"%.1f":"0", 100.0*g/t}')
  [ "$name" = "baseline" ] && base_pct="$pct"
  # real-time if it processed CAP_SECONDS of data in < 0.85*CAP_SECONDS wall
  rt=$(awk -v w="$wall" -v c="$CAP_SECONDS" 'BEGIN{print (w < 0.85*c)?"YES":"NO"}')
  delta=$(awk -v p="$pct" -v b="$base_pct" 'BEGIN{printf "%+.1f", p-b}')
  verdict="$name"
  [ "$rt" = "NO" ] && verdict="TOO SLOW (live-unusable)"
  printf "%-14s %7s%% %8ss %11s   %s\n" "$name" "$pct" "$wall" "$rt" "$verdict (Δ${delta})"
done
echo "----------------------------------------------------------------------"
echo "Pick the highest good% among realtime?=YES rows. capture=${CAP_SECONDS}s,"
echo "so wall must be < $(awk -v c="$CAP_SECONDS" 'BEGIN{printf "%.1f", 0.85*c}')s to run live."
