#!/bin/bash
# skip_detector.sh — detect mpv playback skipping/stuttering via IPC socket.
#
# Polls mpv's time-pos property every N seconds via Unix domain socket
# JSON IPC. Compares delta(time-pos) to delta(wall-clock).
#   rate ≈ 1.0  → playing normally
#   rate > 1.2  → skipping forward (mpv dropping frames to catch up)
#   rate < 0.5  → stalled
#   rate ≈ 0    → frozen
#
# Writes /tmp/skip_state.json so bot can poll.
#
# Run: nohup ~/skip_detector.sh > /tmp/skip_detector.log 2>&1 & disown

set -u
STATE=/tmp/skip_state.json
SOCK=/tmp/mpv_stvt.sock
SAMPLE_SEC=4

# Python helper: get property from mpv IPC socket
get_prop() {
    local prop="$1"
    python3 -c "
import socket, json, sys
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect('$SOCK')
    req = json.dumps({'command': ['get_property', '$prop']}) + '\n'
    s.sendall(req.encode())
    buf = b''
    while True:
        ch = s.recv(4096)
        if not ch: break
        buf += ch
        if b'\n' in buf: break
    s.close()
    for line in buf.split(b'\n'):
        if not line: continue
        d = json.loads(line)
        if d.get('error') == 'success':
            v = d.get('data')
            if v is not None: print(v)
            break
except Exception as e:
    sys.exit(1)
" 2>/dev/null
}

emit() {
    cat > "$STATE" <<EOF
{"ts":"$(date '+%H:%M:%S')","tier":"$1","rate":$2,"mpv_pid":$3,"time_pos":$4,"reason":"$5"}
EOF
}

prev_pos=""
prev_wall=$(date +%s.%N)

while true; do
    sleep $SAMPLE_SEC

    MV=$(pgrep -f 'STVT Player' | head -1)
    if [ -z "$MV" ]; then
        emit "nompv" 0 0 0 "no mpv process"
        prev_pos=""
        continue
    fi

    if [ ! -S "$SOCK" ]; then
        emit "noipc" 0 "$MV" 0 "mpv socket not present"
        continue
    fi

    cur_pos=$(get_prop "time-pos")
    cur_wall=$(date +%s.%N)

    if [ -z "$cur_pos" ]; then
        emit "noipc" 0 "$MV" 0 "no IPC response (mpv may be in probe)"
        continue
    fi

    if [ -z "$prev_pos" ]; then
        prev_pos=$cur_pos
        prev_wall=$cur_wall
        emit "init" 1.0 "$MV" "$cur_pos" "first sample"
        continue
    fi

    # Compute rate
    read tier rate reason <<<$(python3 -c "
prev_pos = $prev_pos
cur_pos = $cur_pos
prev_wall = $prev_wall
cur_wall = $cur_wall
dt = cur_wall - prev_wall
dp = cur_pos - prev_pos
if dt <= 0:
    print('frozen 0 zero-wall-time'); exit()
r = dp / dt
if r > 1.5:
    tier = 'skipping'
elif r > 1.2:
    tier = 'skipping_light'
elif r < 0.05:
    tier = 'frozen'
elif r < 0.5:
    tier = 'stalled'
else:
    tier = 'playing'
print(f'{tier} {r:.3f} dp={dp:.2f}s/dt={dt:.2f}s')
")
    emit "$tier" "$rate" "$MV" "$cur_pos" "$reason"

    prev_pos=$cur_pos
    prev_wall=$cur_wall
done
