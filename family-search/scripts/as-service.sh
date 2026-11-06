#!/usr/bin/env bash
# Run a command as one of the service users, with that service's env file,
# under the same systemd sandbox as the real unit — without printing secrets.
#   as-service.sh web|crawl <command...>
set -euo pipefail
case "${1:-}" in
  web)   USER_=fs-web;   ENVF=/etc/family-search/backend.env; RW="/var/lib/family-search/app /var/lib/family-search/spool /opt/family-search/config";;
  crawl) USER_=fs-crawl; ENVF=/etc/family-search/crawler.env; RW="/var/lib/family-search/crawler /var/lib/family-search/spool";;
  *) echo "usage: $0 web|crawl <command...>" >&2; exit 2;;
esac
shift
exec systemd-run --quiet --wait --pipe --collect --service-type=exec \
  -p User="$USER_" -p Group="$USER_" -p SupplementaryGroups=fsearch \
  -p EnvironmentFile="$ENVF" -p WorkingDirectory=/opt/family-search/backend \
  -p NoNewPrivileges=yes -p ProtectSystem=strict -p ProtectHome=yes -p PrivateTmp=yes \
  -p ReadWritePaths="$RW" "$@"
