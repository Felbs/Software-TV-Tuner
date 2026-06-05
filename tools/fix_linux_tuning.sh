#!/bin/bash
# fix_linux_tuning.sh — make Linux behave like Windows for real-time SDR work.
#
# The single biggest reliability fix for live decode on Linux. Sets the CPU
# governor to "performance", disables USB autosuspend on the SDRplay, raises the
# realtime-priority soft limit, and forbids deep CPU C-states (whose ~400us
# wake-up latency starves the single-threaded matched-filter thread and causes
# OsO sample overflows → drift/freezes).
#
# RUN WITH SUDO, ONCE PER BOOT (the governor resets to schedutil on reboot):
#     sudo tools/fix_linux_tuning.sh
#
# Effects last until reboot. Add to /etc/rc.local or a systemd unit to persist.
set -eu

if [ "$EUID" -ne 0 ]; then echo "Run with sudo: sudo $0" >&2; exit 1; fi

# Real (non-root) user, for the rtprio limits.conf entry.
RUSER="${SUDO_USER:-$(logname 2>/dev/null || echo "")}"

echo "=== 1) CPU governor -> performance ==="
for c in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    [ -w "$c" ] || continue
    echo performance > "$c" 2>/dev/null || true
done
echo "  governor now: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo '?')"

echo ""
echo "=== 2) Disable USB autosuspend on SDRplay (vendor 1df7) ==="
for d in /sys/bus/usb/devices/*/idVendor; do
    [ "$(cat "$d" 2>/dev/null)" = "1df7" ] || continue
    dev=$(dirname "$d")
    echo "on" > "$dev/power/control" 2>/dev/null || true
    echo -1 > "$dev/power/autosuspend_delay_ms" 2>/dev/null || true
    echo "  $dev: control=$(cat "$dev/power/control" 2>/dev/null)"
done

if [ -n "$RUSER" ]; then
echo ""
echo "=== 3) Raise soft rtprio limit to 99 for $RUSER ==="
if grep -qE "^${RUSER}[[:space:]]+soft[[:space:]]+rtprio" /etc/security/limits.conf 2>/dev/null; then
    sed -i -E "s|^(${RUSER}[[:space:]]+soft[[:space:]]+rtprio)[[:space:]]+[0-9]+|\1\t99|" /etc/security/limits.conf
else
    printf '%s\tsoft\trtprio\t99\n%s\thard\trtprio\t99\n' "$RUSER" "$RUSER" >> /etc/security/limits.conf
fi
echo "  $RUSER rtprio soft/hard -> 99 (takes effect on next login)"
fi

echo ""
echo "=== 4) Forbid deep CPU C-states (hold /dev/cpu_dma_latency at 0us) ==="
# While a process holds this PM_QOS handle open with value 0, the kernel keeps
# CPUs in C0/POLL — removing the C-state wake-up latency that causes OsO.
pkill -f "cpu_dma_latency holder" 2>/dev/null || true
sleep 0.2
nohup bash -c 'exec -a "cpu_dma_latency holder" python3 -c "
import os, struct, time
fd = os.open(\"/dev/cpu_dma_latency\", os.O_WRONLY)
os.write(fd, struct.pack(\"i\", 0))
while True: time.sleep(3600)
"' > /tmp/cstate_holder.log 2>&1 &
HOLDER_PID=$!
sleep 0.4
if kill -0 "$HOLDER_PID" 2>/dev/null; then
    echo "$HOLDER_PID" > /tmp/cstate_holder.pid
    echo "  C-state holder PID $HOLDER_PID (kill it to restore normal C-states)"
else
    echo "  WARN: C-state holder died; see /tmp/cstate_holder.log"
fi

echo ""
echo "DONE. Launch the chain, then watch the OsO rate stay near zero."
