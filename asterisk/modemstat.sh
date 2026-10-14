#!/bin/sh
# Read-only status poll of the EC20F over its spare AT port (USB interface 03,
# udev name /dev/quectel-status). chan_quectel owns interface 02
# (/dev/quectel-at), which is also where all URCs (+CMTI, RING...) go, so this
# never steals events. Query commands only - nothing that touches data/bearer.
# The app reads the raw answers from /data/modemstat/status.txt.
DEV=/dev/quectel-status
OUT=/data/modemstat/status.txt
mkdir -p /data/modemstat
while :; do
    if [ -c "$DEV" ]; then
        timeout 20 sh -c '
            exec 3<>"$1" || exit 1
            echo "TS $(date +%s)"
            for c in ATE0 AT+QTEMP "AT+QENG=\"servingcell\"" AT+QSPN AT+QNWINFO "AT+QCFG=\"ims\""; do
                printf "%s\r" "$c" >&3
                timeout 1 cat <&3
            done' _ "$DEV" 2>/dev/null | tr -d '\r' | grep -v '^$' > "$OUT.tmp" && mv "$OUT.tmp" "$OUT"
    fi
    sleep 30
done
