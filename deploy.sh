#!/usr/bin/env bash
# Deploy the app to https://wt.shadowdog.cat
# ("prod" = the VPS host from ~/.ssh/config; Caddy serves /var/www/wt)
set -e
cd "$(dirname "$0")"
tar czf - index.html css js | ssh prod "tar xzf - -C /var/www/wt"
echo "Deployed to https://wt.shadowdog.cat"
