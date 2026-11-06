#!/usr/bin/env bash
# On-CT acceptance checks that need the real network, services and firewall.
# Run as root inside the familysearch CT after install.sh. Read-only.
# Prints PASS/FAIL lines you can paste into TEST-RESULTS.md.
set -uo pipefail
. /etc/family-search/site.env
PASS=0; FAIL=0
ok()   { printf 'PASS  %s\n' "$*"; PASS=$((PASS + 1)); }
bad()  { printf 'FAIL  %s\n' "$*"; FAIL=$((FAIL + 1)); }
skip() { printf 'SKIP  %s\n' "$*"; }
check() { local name=$1; shift; if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi; }
as() { local u=$1; shift; setpriv --reuid="$u" --regid="$u" --clear-groups "$@"; }
set_() { [ -n "${1:-}" ] && [[ "$1" != *__SET_ME__* ]]; }

echo "== services"
for u in meilisearch family-search nftables; do check "$u.service active" systemctl is-active --quiet "$u"; done
for t in family-crawler.timer family-photos-sync.timer family-backup.timer family-crawler.path; do
  check "$t enabled" systemctl is-enabled --quiet "$t"; done

echo "== binding and gate"
check "Meilisearch listens on 127.0.0.1:7700 only" bash -c "ss -ltn | grep -q '127.0.0.1:7700' && ! ss -ltn | grep -Eq '(0.0.0.0|\\*|\\[::\\]):7700'"
check "backend listens on :8080" bash -c "ss -ltn | grep -q ':8080'"
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/api/search?q=test)
[ "$code" = 401 ] && ok "no Access JWT -> 401 (gate works)" || bad "no Access JWT -> $code (expected 401)"
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Cf-Access-Jwt-Assertion: eyJhbGciOiJSUzI1NiIsImtpZCI6IngifQ.eyJlbWFpbCI6ImFAYi5jIn0.c2ln' http://127.0.0.1:8080/api/me)
[ "$code" = 401 ] && ok "forged JWT -> 401" || bad "forged JWT -> $code"
hdrs=$(curl -s -D - -o /dev/null http://127.0.0.1:8080/)
for h in "content-security-policy: default-src 'self'" "x-content-type-options: nosniff" \
         "referrer-policy: no-referrer" "permissions-policy:" "cache-control: private, no-store"; do
  grep -qi "^$h" <<<"$hdrs" && ok "header $h" || bad "header $h"; done
check "no access log of queries (uvicorn --no-access-log)" bash -c "! journalctl -u family-search --since -10min | grep -q 'GET /api/search'"

echo "== firewall"
check "nftables table loaded" nft list table inet familysearch
nft list chain inet familysearch input | grep -q 'policy drop' && ok "input policy drop" || bad "input policy drop"
if set_ "${CLOUDFLARED_IP:-}"; then
  nft list chain inet familysearch input | grep -q "ip saddr $CLOUDFLARED_IP tcp dport 8080 accept" \
    && ok "8080 only from cloudflared host $CLOUDFLARED_IP" || bad "8080 rule for $CLOUDFLARED_IP missing"
else skip "CLOUDFLARED_IP not set — 8080 closed to the LAN"; fi
echo "       (from ANOTHER LAN host, run: curl -m 5 http://<CT-IP>:8080/ — it must time out)"

echo "== outbound per user"
if as fs-crawl curl -s -m 8 -o /dev/null https://www.nasa.gov/robots.txt; then ok "crawler reaches public HTTPS"; else bad "crawler cannot reach public HTTPS"; fi
if as fs-crawl curl -s -m 4 -o /dev/null http://192.168.0.1/; then bad "crawler reached 192.168.0.1"; else ok "crawler blocked from RFC1918 (192.168.0.1)"; fi
if as fs-crawl curl -s -m 4 -o /dev/null http://169.254.169.254/; then bad "crawler reached metadata range"; else ok "crawler blocked from link-local/metadata"; fi
if set_ "${IMMICH_IP:-}"; then
  if as fs-crawl curl -s -m 4 -o /dev/null "http://$IMMICH_IP:2283/api/server/version"; then bad "crawler reached Immich"; else ok "crawler cannot reach Immich"; fi
  if as nobody curl -s -m 4 -o /dev/null "http://$IMMICH_IP:2283/api/server/version"; then bad "other users reach Immich"; else ok "only fs-web may reach Immich"; fi
  v=$(as fs-web curl -s -m 6 "http://$IMMICH_IP:2283/api/server/version")
  [ -n "$v" ] && ok "backend user reaches Immich on 2283 ($v)" || bad "backend user cannot reach Immich on 2283"
else skip "IMMICH_IP not set"; fi
if as meilisearch curl -s -m 4 -o /dev/null https://www.nasa.gov/; then bad "meilisearch user has outbound access"; else ok "meilisearch user has no outbound access"; fi
if as fs-web curl -s -m 8 -o /dev/null https://www.cloudflare.com/; then ok "backend reaches HTTPS (Access signing keys)"; else bad "backend cannot reach HTTPS"; fi

echo "== Immich permissions (restricted user)"
if grep -q '^IMMICH_API_KEY=__SET_ME__' /etc/family-search/backend.env; then skip "IMMICH_API_KEY not set yet"
else /opt/family-search/scripts/as-service.sh web /opt/family-search/venv/bin/python -m familysearch.cli check-immich; fi

echo "== storage"
findmnt -n /var/lib/meilisearch >/dev/null && ok "Meilisearch data on its own mount ($(findmnt -n -o SOURCE /var/lib/meilisearch))" \
  || skip "Meilisearch data is on the root disk"
for f in /etc/family-search/*.env; do
  [ "$(stat -c '%U %a' "$f")" = "root 600" ] && ok "$f root 600" || bad "$f is $(stat -c '%U %a' "$f")"; done

echo; echo "verify: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
