#!/usr/bin/env bash
# Daily refresh of the War Thunder hp/ton mobility snapshot served at
# https://wt.shadowdog.cat. Writes straight into the web root (atomically, via
# build_mobility.py's MOBILITY_OUT override) and appends to a log.
#
# Installed on the VPS at /opt/wt-mobility/refresh.sh alongside a copy of
# build_mobility.py, and triggered by /etc/cron.d/wt-mobility (see
# tools/vps-mobility.cron). Kept here so the server setup is version-controlled.
set -euo pipefail
export MOBILITY_OUT=/var/www/wt/data/mobility.json
LOG=/var/log/wt-mobility.log
cd /opt/wt-mobility
echo "===== $(date -u '+%Y-%m-%d %H:%M:%S UTC') refresh start =====" >> "$LOG"
if /usr/bin/python3 build_mobility.py >> "$LOG" 2>&1; then
  echo "----- done OK -----" >> "$LOG"
else
  echo "!!!!! FAILED (exit $?) — kept previous mobility.json !!!!!" >> "$LOG"
fi
