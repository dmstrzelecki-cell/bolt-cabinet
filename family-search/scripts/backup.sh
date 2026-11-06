#!/usr/bin/env bash
# Nightly backup: config/, app SQLite (consistent .backup copy), parent-uploaded
# library files, and the env files. Keeps the last 14. The search index is NOT
# backed up — it is rebuilt from these (scripts/rebuild-index.sh).
#   backup.sh [label]
set -Eeuo pipefail
DEST=${FS_ROOT:-/opt/family-search}
DATA=${FS_DATA_DIR:-/var/lib/family-search}
ETC=${FS_ETC:-/etc/family-search}
OUT=${FS_BACKUP_DIR:-/var/backups/family-search}
KEEP=${FS_BACKUP_KEEP:-14}
STAMP=$(date +%Y%m%dT%H%M%S)${1:+-$1}
umask 077
mkdir -p "$OUT"
WORK=$(mktemp -d "$OUT/.work.XXXXXX")
cleanup() { rm -rf -- "$WORK"; }
trap cleanup EXIT
mkdir -p "$WORK/b/app"
echo "[1/4] SQLite snapshot"
if [ -f "$DATA/app/app.db" ]; then sqlite3 "$DATA/app/app.db" ".backup '$WORK/b/app/app.db'"; fi
echo "[2/4] config + library files"
cp -a "$DEST/config" "$WORK/b/config"
if [ -d "$DATA/app/library" ]; then cp -a "$DATA/app/library" "$WORK/b/app/library"; fi
echo "[3/4] env files"
if [ -d "$ETC" ]; then cp -a "$ETC" "$WORK/b/etc"; fi
echo "[4/4] archive"
tar -C "$WORK/b" -czf "$OUT/family-search-$STAMP.tar.gz" .
chmod 600 "$OUT/family-search-$STAMP.tar.gz"
ls -1t "$OUT"/family-search-*.tar.gz | tail -n +$((KEEP + 1)) | while read -r old; do rm -f -- "$old"; done
echo "backup: $OUT/family-search-$STAMP.tar.gz ($(du -h "$OUT/family-search-$STAMP.tar.gz" | cut -f1))"
