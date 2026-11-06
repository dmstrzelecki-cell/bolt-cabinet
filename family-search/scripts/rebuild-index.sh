#!/usr/bin/env bash
# Throw away the Meilisearch indexes and rebuild them from the sources of truth:
# config/, the app SQLite (library + photos) and the crawler's page store.
# Nothing a parent entered is lost — the index is only a cache.
set -Eeuo pipefail
DEST=/opt/family-search
KEY=$(grep '^MEILI_MASTER_KEY=' /etc/family-search/backend.env | cut -d= -f2-)
echo "[1/5] pausing crawler"
systemctl stop family-crawler.service family-crawler.path 2>/dev/null || true
echo "[2/5] deleting indexes"
for idx in web_pages library_items photos; do
  curl -fsS -X DELETE -H "Authorization: Bearer $KEY" "http://127.0.0.1:7700/indexes/$idx" >/dev/null || true
  printf '       %s\n' "$idx"
done
unset KEY
sleep 2
echo "[3/5] library + photos from SQLite"
"$DEST/scripts/as-service.sh" web "$DEST/venv/bin/python" -m familysearch.cli reindex-local
echo "[4/5] web pages from the crawler's page store"
"$DEST/scripts/as-service.sh" crawl "$DEST/venv/bin/python" "$DEST/crawler/crawler.py" --reindex
echo "[5/5] resuming crawler"
systemctl start family-crawler.path
echo "index rebuilt"
