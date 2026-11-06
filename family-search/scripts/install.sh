#!/usr/bin/env bash
# Family Search installer — run as root INSIDE the new familysearch CT.
# Repeatable: safe to re-run for updates. Never overwrites live config,
# data, or existing secrets. Prints no secrets.
#
#   bash /root/family-search-src/scripts/install.sh
#
# The source tree (this script's parent directory) is synced into
# /opt/family-search; live config in /opt/family-search/config is only seeded
# from the defaults when a file is missing.
set -Eeuo pipefail
trap 'echo; echo "!! install failed at line $LINENO — nothing after this step ran." >&2' ERR

SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DEST=/opt/family-search
ETC=/etc/family-search
DATA=/var/lib/family-search
MEILI_DATA=/var/lib/meilisearch
TOTAL=14
N=0
step() { N=$((N + 1)); printf '\n\033[1m[%2d/%d] %s\033[0m\n' "$N" "$TOTAL" "$*"; }
note() { printf '       %s\n' "$*"; }
placeholder() { [ -z "${1:-}" ] || [[ "$1" == *__SET_ME__* ]]; }

[ "$(id -u)" = 0 ] || { echo "run as root inside the CT" >&2; exit 1; }
grep -q 'VERSION_CODENAME=bookworm' /etc/os-release || { echo "expected Debian 12 (bookworm)" >&2; exit 1; }
[ "$(uname -m)" = x86_64 ] || { echo "expected x86_64" >&2; exit 1; }

step "Debian packages (and automatic security updates)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends python3 python3-venv ca-certificates curl \
  nftables sqlite3 rsync unattended-upgrades jq iproute2 tar gzip openssl
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now unattended-upgrades.service >/dev/null 2>&1 || true

step "Service users (fs-web, fs-crawl, meilisearch) and shared group"
getent group fsearch >/dev/null || groupadd --system fsearch
for u in fs-web fs-crawl meilisearch; do
  id "$u" >/dev/null 2>&1 || useradd --system --user-group --no-create-home --home-dir /nonexistent \
    --shell /usr/sbin/nologin "$u"
done
usermod -aG fsearch fs-web
usermod -aG fsearch fs-crawl

step "Sync code to $DEST (live config is preserved)"
mkdir -p "$DEST"
if [ "$SRC" != "$DEST" ]; then
  rsync -a --delete --info=progress2 --exclude config/ --exclude venv/ --exclude PREFLIGHT.md \
    --exclude TEST-RESULTS.md --exclude tests/e2e/shots/ "$SRC"/ "$DEST"/
  for f in PREFLIGHT.md TEST-RESULTS.md; do
    if [ ! -e "$DEST/$f" ] && [ -e "$SRC/$f" ]; then cp "$SRC/$f" "$DEST/$f"; fi
  done
fi
mkdir -p "$DEST/config"
for f in "$SRC"/config/*; do
  b=$(basename "$f")
  if [ ! -e "$DEST/config/$b" ]; then cp "$f" "$DEST/config/$b"; note "seeded config/$b"; fi
done
chown -R root:root "$DEST"
find "$DEST" -path "$DEST/config" -prune -o -path "$DEST/venv" -prune -o -type d -exec chmod 755 {} +
find "$DEST" -path "$DEST/config" -prune -o -path "$DEST/venv" -prune -o -type f -exec chmod 644 {} +
chmod 755 "$DEST"/scripts/*.sh
chown -R fs-web:fsearch "$DEST/config"
chmod 2770 "$DEST/config"
find "$DEST/config" -type f -exec chmod 660 {} +

step "Data directories"
install -d -o fs-web -g fs-web -m 750 "$DATA/app" "$DATA/app/library"
install -d -o fs-crawl -g fs-crawl -m 750 "$DATA/crawler"
install -d -o fs-web -g fsearch -m 2770 "$DATA/spool"
install -d -o root -g root -m 755 "$DATA"
install -d -o meilisearch -g meilisearch -m 750 "$MEILI_DATA"
install -d -o root -g root -m 700 /var/backups/family-search
if findmnt -n "$MEILI_DATA" >/dev/null; then note "$MEILI_DATA is its own mount (SSD mount point): $(findmnt -n -o SOURCE "$MEILI_DATA")"
else note "WARNING: $MEILI_DATA is on the root disk, not a separate SSD mount (see PREFLIGHT.md)"; fi

step "Site settings ($ETC/site.env)"
install -d -o root -g root -m 755 "$ETC"
[ -e "$ETC/site.env" ] || install -o root -g root -m 644 "$SRC/scripts/site.env.example" "$ETC/site.env"
# shellcheck disable=SC1091
. "$ETC/site.env"

step "Secrets (generated here, root-only, never printed)"
setkey() {  # setkey FILE KEY VALUE — only adds keys that are missing
  local file=$1 key=$2 val=$3
  touch "$file"; chown root:root "$file"; chmod 600 "$file"
  grep -q "^${key}=" "$file" || printf '%s=%s\n' "$key" "$val" >> "$file"
}
MEILI_KEY=$(grep -s '^MEILI_MASTER_KEY=' "$ETC/meilisearch.env" | cut -d= -f2- || true)
[ -n "$MEILI_KEY" ] || MEILI_KEY=$(openssl rand -hex 32)
setkey "$ETC/meilisearch.env" MEILI_MASTER_KEY "$MEILI_KEY"
for f in backend crawler; do
  setkey "$ETC/$f.env" MEILI_MASTER_KEY "$MEILI_KEY"
  setkey "$ETC/$f.env" MEILI_URL "http://127.0.0.1:7700"
  setkey "$ETC/$f.env" FS_ROOT "$DEST"
  setkey "$ETC/$f.env" FS_DATA_DIR "$DATA"
  setkey "$ETC/$f.env" FS_PUBLIC_ORIGIN "${PUBLIC_ORIGIN:-https://search.thestrzs.net}"
done
setkey "$ETC/backend.env" CSRF_SECRET "$(openssl rand -hex 32)"
setkey "$ETC/backend.env" CF_ACCESS_TEAM_DOMAIN "__SET_ME__"
setkey "$ETC/backend.env" CF_ACCESS_AUD "__SET_ME__"
setkey "$ETC/backend.env" PARENT_EMAILS "__SET_ME__"
setkey "$ETC/backend.env" ALLOWED_EMAILS ""
setkey "$ETC/backend.env" IMMICH_URL "__SET_ME__"
setkey "$ETC/backend.env" IMMICH_API_KEY "__SET_ME__"
setkey "$ETC/backend.env" NOTIFY_URL "__SET_ME__"
setkey "$ETC/backend.env" NOTIFY_FORMAT "ntfy"
setkey "$ETC/crawler.env" FS_CRAWL_MAX_MINUTES "240"
unset MEILI_KEY
note "secrets live in $ETC/{meilisearch,backend,crawler}.env (mode 600)"

step "Meilisearch ${MEILI_VERSION} (checksum verified)"
BIN=/usr/local/bin/meilisearch-${MEILI_VERSION}
if [ ! -x "$BIN" ]; then
  URL="https://github.com/meilisearch/meilisearch/releases/download/${MEILI_VERSION}/meilisearch-linux-amd64"
  WANT=${MEILI_SHA256:-}
  if [ -z "$WANT" ]; then
    WANT=$(curl -fsS "https://api.github.com/repos/meilisearch/meilisearch/releases/tags/${MEILI_VERSION}" \
      | jq -r '.assets[] | select(.name=="meilisearch-linux-amd64") | .digest // empty' | sed 's/^sha256://')
  fi
  [ -n "$WANT" ] || { echo "No published sha256 for ${MEILI_VERSION}. Set MEILI_SHA256 in $ETC/site.env." >&2; exit 1; }
  TMPB=$(mktemp)
  curl -fL --progress-bar -o "$TMPB" "$URL"
  GOT=$(sha256sum "$TMPB" | cut -d' ' -f1)
  if [ "$GOT" != "$WANT" ]; then rm -f -- "$TMPB"; echo "Meilisearch checksum MISMATCH — not installed" >&2; exit 1; fi
  install -o root -g root -m 755 "$TMPB" "$BIN"
  rm -f -- "$TMPB"
  note "verified sha256 $GOT"
fi
ln -sfn "$BIN" /usr/local/bin/meilisearch
note "$(/usr/local/bin/meilisearch --version)"

step "Python environment (pinned, hash-checked)"
[ -x "$DEST/venv/bin/python" ] || python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -q --upgrade --require-hashes --only-binary=:all: --no-deps \
  --progress-bar on -r "$DEST/backend/requirements.lock"
chown -R root:root "$DEST/venv"

step "systemd units"
install -o root -g root -m 644 "$DEST"/systemd/*.service "$DEST"/systemd/*.timer "$DEST"/systemd/*.path \
  /etc/systemd/system/
systemctl daemon-reload

step "Firewall (nftables, default deny)"
install -d -m 755 /etc/nftables.d
APP_RULE="# 8080 closed: CLOUDFLARED_IP not set in site.env"
placeholder "${CLOUDFLARED_IP:-}" || APP_RULE="ip saddr ${CLOUDFLARED_IP} tcp dport 8080 accept"
SSH_RULE="# SSH closed: ADMIN_SSH_SOURCE not set (use pct enter)"
placeholder "${ADMIN_SSH_SOURCE:-}" || SSH_RULE="ip saddr ${ADMIN_SSH_SOURCE} tcp dport 22 accept"
RENDER=$(sed -e "s|@APP_RULE@|${APP_RULE}|" -e "s|@SSH_RULE@|${SSH_RULE}|" \
             -e "s|@DNS_IP@|${DNS_IP:-192.168.0.40}|g" "$DEST/nftables/family-search.nft.in")
if placeholder "${IMMICH_IP:-}"; then RENDER=$(grep -v '@IMMICH_IP@' <<<"$RENDER")
else RENDER=${RENDER//@IMMICH_IP@/$IMMICH_IP}; fi
if placeholder "${NOTIFY_LAN_TARGET:-}"; then RENDER=$(grep -v '@NOTIFY_IP@' <<<"$RENDER")
else RENDER=${RENDER//@NOTIFY_IP@/${NOTIFY_LAN_TARGET%:*}}; RENDER=${RENDER//@NOTIFY_PORT@/${NOTIFY_LAN_TARGET##*:}}; fi
printf '%s\n' "$RENDER" > /etc/nftables.d/family-search.nft
nft -c -f /etc/nftables.d/family-search.nft
grep -q 'include "/etc/nftables.d/\*.nft"' /etc/nftables.conf 2>/dev/null || \
  printf '\ninclude "/etc/nftables.d/*.nft"\n' >> /etc/nftables.conf
nft -c -f /etc/nftables.conf
systemctl enable --now nftables.service >/dev/null
nft -f /etc/nftables.d/family-search.nft
note "inbound: ${APP_RULE}"
note "         ${SSH_RULE}"

step "Start Meilisearch and create indexes"
systemctl enable --now meilisearch.service
for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:7700/health >/dev/null 2>&1 && break; printf '.'; sleep 1; done; echo
"$DEST/scripts/as-service.sh" web "$DEST/venv/bin/python" -m familysearch.cli ensure-indexes

step "Start the backend, crawler timer, photo sync and backups"
systemctl enable --now family-search.service family-crawler.timer family-crawler.path \
  family-photos-sync.timer family-backup.timer
systemctl restart family-search.service

step "Self-check"
sleep 2
CODE=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/api/me || true)
case "$CODE" in
  401) note "backend answers 401 without an Access token — the gate works";;
  503) note "backend answers 503: Access not configured yet (fails closed — expected until placeholders are filled)";;
  *)   note "UNEXPECTED: /api/me returned $CODE without a token"; exit 1;;
esac
ss -ltn | grep -q '127.0.0.1:7700' && ! ss -ltn | grep -qE '(0\.0\.0\.0|\*|\[::\]):7700' \
  && note "Meilisearch is bound to 127.0.0.1 only" || { note "Meilisearch bind check FAILED"; exit 1; }

step "Placeholders David still has to fill"
MISSING=0
for kv in "backend.env:CF_ACCESS_TEAM_DOMAIN" "backend.env:CF_ACCESS_AUD" "backend.env:PARENT_EMAILS" \
          "backend.env:IMMICH_URL" "backend.env:IMMICH_API_KEY" "site.env:CLOUDFLARED_IP" \
          "site.env:IMMICH_IP" "site.env:ADMIN_SSH_SOURCE"; do
  f=${kv%%:*}; k=${kv#*:}
  if grep -q "^${k}=.*__SET_ME__" "$ETC/$f" || ! grep -q "^${k}=" "$ETC/$f"; then
    note "• $ETC/$f  $k"; MISSING=1
  fi
done
grep -q '^NOTIFY_URL=__SET_ME__' "$ETC/backend.env" && \
  note "(optional) $ETC/backend.env  NOTIFY_URL — where flagged-activity notifications go"
[ $MISSING = 0 ] && note "none — run scripts/verify.sh next" || \
  note "Edit those, then re-run this installer (or: systemctl restart family-search)."
echo; echo "Done."
