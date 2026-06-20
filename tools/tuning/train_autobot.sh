#!/usr/bin/env bash
# TRAINING AUTOBOT — for each channel on the current antenna:
#   1. capture a short IQ (record_iq self-exits, no SDR orphan risk)
#   2. replay it through each equalizer config (file-based, no SDR)
#   3. score good-packet% and wall-time (real-time feasibility)
# Builds the per-channel tuning dataset for the auto-tuner, then a summary
# showing whether the best real-time config is the same across channels.
set -u
REPO="$HOME/Software-TV-Tuner"
TRAIN="$HOME/train_data"
mkdir -p "$TRAIN"
cd "$REPO" || exit 1

CHANNELS=(${CHANNELS:-7 9 21 31 34 36})
SECS="${SECS:-20}"
IFGR="${IFGR:-30}"

BASE="STVT_RS=stock STVT_VITERBI=hard STVT_EQ=long STVT_SPS=1.1 STVT_RRC_SYMS=4 \
STVT_FPLL_FOLD=1 STVT_FPLL_BLOCK_NCO=1"
# focused config set (full sweep was too slow per-channel)
CONFIGS=(
  "baseline|"
  "erasure|STVT_RS=erasure"
  "beta_2e-5|STVT_EQ_BETA=2e-5"
  "dd_track|STVT_EQ_DD_MU=1e-3 STVT_EQ_DD_GATE=1"
  "rls|STVT_EQ_RLS=1"
)

count_good() {
python3 - "$1" <<'PY'
import sys
p=sys.argv[1]; good=null=bad=0
try: data=open(p,'rb').read()
except FileNotFoundError: print("0 0"); sys.exit()
for i in range(len(data)//188):
    pkt=data[i*188:i*188+188]
    if pkt[0]!=0x47: bad+=1; continue
    pid=((pkt[1]&0x1f)<<8)|pkt[2]
    null += pid==0x1fff; good += pid!=0x1fff
tot=good+null+bad
print(f"{(100.0*good/tot if tot else 0):.1f} {good}")
PY
}

SUMMARY="$TRAIN/SUMMARY.txt"
: > "$SUMMARY"
echo "TRAINING AUTOBOT — antenna run  (IFGR=$IFGR, ${SECS}s captures)" | tee -a "$SUMMARY"
echo "channels: ${CHANNELS[*]}" | tee -a "$SUMMARY"
echo "=========================================================" | tee -a "$SUMMARY"

for ch in "${CHANNELS[@]}"; do
  iq="$TRAIN/iq_rf${ch}.cs16"
  echo "" | tee -a "$SUMMARY"
  echo "### RF $ch ###" | tee -a "$SUMMARY"
  # make sure the SDR is free (no live chain) before capturing
  for p in $(pgrep -f 'tv_live.py' 2>/dev/null); do kill -TERM "$p" 2>/dev/null; done
  sleep 2
  echo "[autobot $(date +%H:%M:%S)] capturing RF$ch (${SECS}s)..."
  python3 tools/record_iq.py --rf "$ch" --seconds "$SECS" --ifgr "$IFGR" \
    --rfgain-sel 5 --antenna "Antenna A" --format cs16 --out "$iq" >/dev/null 2>&1
  if [ ! -s "$iq" ]; then echo "  capture FAILED (no data) — skipping" | tee -a "$SUMMARY"; continue; fi
  base_pct=""; best_rt_name="baseline"; best_rt_pct="0"
  printf "  %-12s %7s %8s %s\n" "config" "good%" "wall" "realtime?" | tee -a "$SUMMARY"
  for entry in "${CONFIGS[@]}"; do
    name="${entry%%|*}"; extra="${entry#*|}"; out="$TRAIN/.tmp.ts"
    t0=$(date +%s.%N)
    env $BASE $extra python3 tools/tv_replay.py --iq "$iq" --out "$out" \
        --log "$TRAIN/.tmp.log" >/dev/null 2>&1
    t1=$(date +%s.%N)
    wall=$(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.1f", b-a}')
    read pct good < <(count_good "$out"); rm -f "$out"
    [ "$name" = "baseline" ] && base_pct="$pct"
    # real-time: within 1.3x of baseline wall (baseline is the live reference)
    rt="YES"
    awk -v w="$wall" -v c="$SECS" 'BEGIN{exit !(w > 1.4*c)}' && rt="NO(slow)"
    if [ "$rt" = "YES" ] && awk -v p="$pct" -v b="$best_rt_pct" 'BEGIN{exit !(p>b)}'; then
      best_rt_pct="$pct"; best_rt_name="$name"
    fi
    printf "  %-12s %6s%% %7ss %s\n" "$name" "$pct" "$wall" "$rt" | tee -a "$SUMMARY"
  done
  echo "  -> best REAL-TIME config: $best_rt_name ($best_rt_pct%, baseline $base_pct%)" | tee -a "$SUMMARY"
  rm -f "$iq"   # reclaim disk
done
echo "" | tee -a "$SUMMARY"
echo "=== AUTOBOT DONE — see $SUMMARY ===" | tee -a "$SUMMARY"
