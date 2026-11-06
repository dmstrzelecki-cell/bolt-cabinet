#!/usr/bin/env bash
# Restore sites, tiles, albums, filters, library entries/files and settings
# from a backup made by backup.sh, then rebuild the local parts of the index.
# Takes a safety backup of the current state first.
#   restore.sh /var/backups/family-search/family-search-<stamp>.tar.gz [--with-secrets]
set -Eeuo pipefail
ARCHIVE=${1:?usage: restore.sh <backup.tar.gz> [--with-secrets]}
WITH_SECRETS=${2:-}
DEST=${FS_ROOT:-/opt/family-search}
DATA=${FS_DATA_DIR:-/var/lib/family-search}
ETC=${FS_ETC:-/etc/family-search}
SYSTEMD=${FS_SYSTEMD:-1}
[ -f "$ARCHIVE" ] || { echo "no such file: $ARCHIVE" >&2; exit 1; }
WORK=$(mktemp -d)
cleanup() { rm -rf -- "$WORK"; }
trap cleanup EXIT
echo "[1/6] checking archive"
tar -tzf "$ARCHIVE" | grep -qx './config/allowlist.yaml' || { echo "not a family-search backup" >&2; exit 1; }
tar -C "$WORK" -xzf "$ARCHIVE" --no-same-owner
echo "[2/6] safety backup of the current state"
"$(dirname "$0")/backup.sh" pre-restore
if [ "$SYSTEMD" = 1 ]; then
  echo "[3/6] stopping services"
  systemctl stop family-search.service family-crawler.service family-photos-sync.service 2>/dev/null || true
else echo "[3/6] (not stopping services)"; fi
echo "[4/6] restoring config, database, library"
rm -rf -- "$DEST/config.restoring"
cp -a "$WORK/config" "$DEST/config.restoring"
rm -rf -- "$DEST/config.old"
mv "$DEST/config" "$DEST/config.old"
mv "$DEST/config.restoring" "$DEST/config"
rm -rf -- "$DEST/config.old"
mkdir -p "$DATA/app"
if [ -f "$WORK/app/app.db" ]; then
  rm -f -- "$DATA/app/app.db-wal" "$DATA/app/app.db-shm"
  cp "$WORK/app/app.db" "$DATA/app/app.db"
fi
rm -rf -- "$DATA/app/library"
if [ -d "$WORK/app/library" ]; then cp -a "$WORK/app/library" "$DATA/app/library"; else mkdir -p "$DATA/app/library"; fi
if [ "$WITH_SECRETS" = "--with-secrets" ] && [ -d "$WORK/etc" ]; then
  cp -a "$WORK/etc"/. "$ETC"/; chmod 600 "$ETC"/*.env; echo "       env files restored"
fi
if id fs-web >/dev/null 2>&1 && getent group fsearch >/dev/null; then
  chown -R fs-web:fsearch "$DEST/config"; chmod 2770 "$DEST/config"
  find "$DEST/config" -type f -exec chmod 660 {} +
  chown -R fs-web:fs-web "$DATA/app"
fi
if [ "$SYSTEMD" = 1 ]; then
  echo "[5/6] starting services and rebuilding library/photo index"
  systemctl start family-search.service
  "$DEST/scripts/as-service.sh" web "$DEST/venv/bin/python" -m familysearch.cli reindex-local
  echo "[6/6] queueing a crawl to re-check web pages"
  install -o fs-web -g fsearch -m 660 /dev/null "$DATA/spool/crawl-now.tmp"
  echo '{"hosts": null, "requested_by": "restore.sh"}' > "$DATA/spool/crawl-now.tmp"
  mv "$DATA/spool/crawl-now.tmp" "$DATA/spool/crawl-now"
else echo "[5/6] [6/6] skipped (FS_SYSTEMD=0)"; fi
echo "restored from $ARCHIVE"
