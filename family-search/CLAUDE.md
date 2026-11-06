# Family Search — project context

A private, kid-safe search engine at **search.thestrzs.net**. It searches only
parent-approved websites, a parent-curated library, and selected Immich photo
albums. Primary user: David's 11-year-old daughter, on Chrome on a phone with
Google Family Link set to "Only allow approved sites".

**Done means:** she opens one URL, searches approved content, taps a tile,
views allowed family photos, and never gets a result outside the approved list.

This directory is self-contained. It lives inside the `bolt-cabinet` repo for
now (the only repo this was built in); it shares nothing with the bolt app and
deploys to its own CT at `/opt/family-search`.

## Non-negotiable rules (stop and report to David if one can't be met)
- **Scope:** work only in the new CT and `/opt/family-search`. Never modify
  CT100/101/102/104/199/300/304, the Pi, UniFi, Cloudflare, ZFS pools, or
  Immich settings. Never read `tank/data` or Immich ingestion sources. Never
  connect to a private Immich instance. Don't write outside the CT's storage.
  Long-running commands show a progress bar or counter. No tmux/screen.
- **Child safety:** default deny — a result appears only if its final URL is
  on the approved list. No live web search, fallback engine, or AI answers.
  No accounts, comments, chat, uploads, sharing, or profiles for the child.
  No stored search history unless David turns it on at `/parent`. No family
  data to third parties.
- **Secrets:** generated on the CT into root-owned mode-600 env files. Never
  committed, printed, or logged. Values David must supply are `__SET_ME__`
  placeholders — report them, never ask for them in chat.
- **Photos:** only the restricted `family-search` Immich account. Never
  originals, faces, people, map, or location endpoints. Never return Immich
  URLs, asset paths, EXIF, GPS, or API keys to the browser.

## Layout
```
backend/familysearch/   FastAPI app (the ONLY thing the browser talks to)
  main.py      routes + the Gate middleware (JWT, parent check, CSRF, headers)
  auth.py      Cloudflare Access JWT verification, CSRF tokens, rate limiter
  config.py    config/*.yaml|json loaders, validation, atomic writes + .history backups
  search.py    Meilisearch queries + post-filter against CURRENT config
  photos.py    restricted Immich client, sync, opaque ids, Pillow re-encode
  library.py   parent links / notes / PDFs (stored outside the web root)
  parent.py    /parent actions; every change audited
  cli.py       ensure-indexes | sync-photos | reindex-local | check-immich
  requirements.lock   hash-pinned wheels for Debian 12 / CPython 3.11
crawler/                separate user; stdlib fetcher with DNS pinning
  safefetch.py exact-host, https-only, private-IP-refusing fetcher
  extract.py   HTML -> text, URL normalisation, skip rules
  crawler.py   scheduling, robots, sitemaps, pacing, page store (crawl.db)
  wikidump.py  simple.wikipedia.org from the official Wikimedia dump
frontend/               index.html/app.js/app.css, parent.*, icons (no third-party anything)
config/                 DEFAULTS. Live copies on the CT are the source of truth.
systemd/  nftables/  scripts/  tests/
```

## Runtime layout on the CT
| Path | Owner | What |
|---|---|---|
| `/opt/family-search` | root, read-only | code + venv |
| `/opt/family-search/config` | fs-web:fsearch 2770 | **live** allowlist/tiles/albums/filters/theme; `.history/` dated copies |
| `/var/lib/family-search/app` | fs-web | `app.db` (SQLite), `library/` PDFs |
| `/var/lib/family-search/crawler` | fs-crawl | `crawl.db` page store (searchable text) |
| `/var/lib/family-search/spool` | fs-web:fsearch 2770 | `crawl-now` trigger, `crawl-status.json` |
| `/var/lib/meilisearch` | meilisearch | index data (SSD mount point if available) |
| `/etc/family-search/*.env` | root 600 | secrets + placeholders |
| `/etc/family-search/site.env` | root 644 | non-secret site settings for install.sh |
| `/var/backups/family-search` | root 700 | nightly backups, last 14 |

Services: `meilisearch` (127.0.0.1:7700), `family-search` (:8080, uvicorn,
**no access log** so queries are never logged), `family-crawler.timer` +
`.path`, `family-photos-sync.timer`, `family-backup.timer`.

## How the safety properties are enforced
- **Identity:** every request (pages, static, API) needs a valid
  `Cf-Access-Jwt-Assertion`: RS256 signature against the team's published
  keys, `aud` = `CF_ACCESS_AUD`, `iss` = `https://<team domain>`, `exp`. No other
  header is trusted. Placeholders unset → every request 503 (fails closed).
- **Roles:** email in `PARENT_EMAILS` → parent. `/parent`, `/api/parent/*` and
  `/static/parent*` are parent-only. Optional `ALLOWED_EMAILS` narrows who
  else may use it at all.
- **Writes:** same-origin `Origin` (or `Sec-Fetch-Site: same-origin`) **and**
  `X-CSRF-Token` = HMAC(CSRF_SECRET, email). Every parent change → audit row.
- **Default deny on output:** search filters Meilisearch by `host IN [approved]`
  *and then* re-checks every hit: `https://`, port 443, hostname == the doc's
  host, host enabled in `allowlist.yaml` right now. Disabling/removing a host
  hides it on the next request even if the index still has its documents.
- **Highlights** travel as U+E000/U+E001 markers, stripped from crawled text at
  index time; the UI turns them into `<mark>` via DOM nodes. Crawled text is
  only ever inserted with `textContent`. Strict CSP (no inline script/style).
- **Photos:** opaque random ids; every thumbnail request re-checks the SQLite
  row *and* that the album is enabled in `photo_albums.yaml`. Immich client
  refuses any path not in `ALLOWED_PATHS`. Images re-encoded into a fresh
  JPEG (no EXIF/GPS/XMP/ICC).
- **Crawler:** exact hostnames; https only; every redirect hop re-validated
  before it is fetched; DNS resolved and every address checked (RFC1918,
  loopback, link-local, CGNAT, metadata, v6 equivalents) and the socket pinned
  to that IP with SNI + cert check; robots.txt obeyed (blocked → `blocked`);
  challenges reported (`challenged`), never solved; ≥5 s per host (or
  Crawl-delay); depth 4; page caps; HTML only, 5 MB / 20 s; skip rules;
  tracking params stripped; canonical URLs.
- **Network:** nftables default-deny inbound (8080 only from the cloudflared
  host, SSH only from `ADMIN_SSH_SOURCE`); per-user egress (crawler: DNS +
  public 80/443; backend: DNS + Immich:2283 + public 443; meilisearch: none;
  only fs-web may reach Immich).

## Tiles
Each tile is **Broad** (`filter`/`search`: results shown inside our search,
title + snippet, one tap opens the real page) or **Concise** (`open_url`: the
tile opens one page directly), plus `photos` and `random`. `/parent` flips
between them; a tile keeps both its broad settings and its `url`. A concise
tile is only sent to the browser while its URL's host is approved. Legacy
`open_host` is normalised to `open_url` on load.

A broad tile's topic is the **union** of `categories` (whole sites in
those categories), `hosts` (whole sites) and `sections` (parts of sites: every
page whose URL sits under a link like `https://www.ducksters.com/history/`).
The crawler indexes each page with `prefixes` — its `host/seg/…/` ancestors —
so a section is a plain Meilisearch filter and pagination stays correct; the
backend re-checks every hit against the same scope. Sections on unapproved
hosts are ignored (default deny). `/parent` shows how many pages each tile
reaches, so a mistyped section shows up as "finds 0 pages". Changing the
indexed document shape means bumping `DOC_VERSION` in `crawler.py`.

## Source of truth
`config/` + `app.db` + `crawl.db` + library files. The Meilisearch index is a
cache: `scripts/rebuild-index.sh` recreates it. Never edit the index directly.

## Don'ts
- Don't add a fallback search, suggestions from an outside provider, or AI answers.
- Don't trust any header but the Access JWT for identity.
- Don't render server text as HTML; don't add inline scripts/styles (CSP).
- Don't add third-party fonts, scripts, analytics, maps, or remote images.
- Don't call Immich endpoints beyond `ALLOWED_PATHS`; never originals.
- Don't log queries, tokens, secrets, or photo URLs. Uvicorn runs `--no-access-log`.
- Don't commit anything from `/etc/family-search` or `/var/lib/*`.
- Don't touch Cloudflare, Family Link, or Immich settings — those are David's manual steps.

## Tests
`pytest tests/` (65 tests, fakes for Meilisearch/Immich/web) and
`tests/e2e/run.sh` (Chromium at 360 px). On the CT: `scripts/verify.sh`.
Results go in `TEST-RESULTS.md`.
