#!/usr/bin/env bash
# Deploy the app to https://wt.shadowdog.cat
# ("prod" = the VPS host from ~/.ssh/config; Caddy serves /var/www/wt)
# Also syncs the offline mobility/gunstats builder used by the daily cron
# at /opt/wt-mobility so pen/armor rebuilds don't regress after deploy.
set -e
cd "$(dirname "$0")"
tar czf - index.html css js data | ssh prod "tar xzf - -C /var/www/wt"
scp tools/build_mobility.py prod:/opt/wt-mobility/build_mobility.py
scp tools/vps-mobility-refresh.sh prod:/opt/wt-mobility/refresh.sh
ssh prod "chmod +x /opt/wt-mobility/refresh.sh"
echo "Deployed to https://wt.shadowdog.cat (+ synced /opt/wt-mobility builder)"
