# Bolt Cabinet Lookup — project context

Fast **last-4 part-number → bin-location** lookup for the Penske fastener
cabinets. Runs today as a static single-file page; deploys to the shop **T320**
(a Proxmox LXC container, `boltsapp`, `192.168.0.126`) as a small stdlib Python
server that also persists back-stock box counts and gates access with a
per-employee PIN.

## Layout
- `public/` — what gets served
  - `index.html` — single-file app (search UI + PIN login screen). Tries
    `/api/session` then `/api/bins` at runtime; falls back to static `bins.json`,
    then an embedded copy, so it also works opened straight off disk/USB
    (read-only in that mode, no login prompt — see **Auth**).
  - `bins.json` — **the data master. Edit this to change inventory.** One record per bin.
  - `images/` — optional fastener photos named `<partnumber>.jpg` (`.png`/`.webp`
    also work); auto-shown when a part is found.
- `server/server.py` — T320 deploy server (stdlib only). Serves `public/` + a
  small JSON API for box counts + PIN auth. See **Going live** and **Auth**.
- `state.json` — live box counts, created & owned by the server. **Gitignored.**
- `employees.json` — PIN → `{name, role}` registry, T320-only. **Gitignored,
  never commit** (contains employee IDs/names). Seed template used locally
  during dev; real roster lives only on the container.
- `audit.log` — JSONL, one line per box/bin-full/add-part change (who, bin,
  before/after or pn, timestamp). Server-created, append-only. **Gitignored.**
- `additions.json` — new-part records added live from the app (`+ Add new
  part`), same shape as a `bins.json` record. Server-created/owned, kept
  separate from the curated file so the container's data never drifts from
  what's in git. **Gitignored.** `merged()` combines this with `bins.json`
  before responding to `/api/bins`.
- `PUNCHLIST.md` — open data gaps to close before go-live.

## Deploy topology
- Code lives at `github.com/dmstrzelecki-cell/bolt-cabinet` (private). Push
  from a dev machine with the write-access deploy key; the T320 container
  pulls with a separate **read-only** deploy key
  (`/opt/bolt-cabinet/.git_deploy_key`, owned by `boltapp`, excluded via
  `.git/info/exclude` — not the tracked `.gitignore`, since it's container-local).
- To ship an update: push from dev, then on the T320 run
  `su boltapp -s /bin/bash -c 'cd /opt/bolt-cabinet && git pull'`.
  No service restart needed for front-end/data changes — `server.py` reads
  files fresh every request. Restart (`systemctl restart bolt-cabinet`) only
  if `server.py` itself changes.
- Runs as `systemd` unit `bolt-cabinet`, under unprivileged user `boltapp`,
  enabled on boot. Reachable on the shop LAN at `192.168.0.126:8080` and
  remotely through a Cloudflare Tunnel (Cloudflare Access policy scopes who
  can reach the tunnel hostname at all — separate from the app's own PIN gate).

## Auth
Every employee has a 6-digit PIN (their employee ID) in `employees.json`:
`{"123456": {"name": "Jane Doe", "role": "editor"}}`. Roles: `editor` (full
read/write, the default) or `viewer` (read-only — write buttons render
disabled with a "View only" note). Change someone's access by editing their
`role` in `employees.json` on the container; no code change needed.
- `GET /api/session` → `{authenticated, name, role}`.
- `POST /api/login {pin}` → sets an HttpOnly session cookie (12h TTL) on
  success; 401 on bad PIN; 429 + `retryAfter` after 5 failed attempts from
  the same client (rate-limit key prefers `CF-Connecting-IP` over the raw
  socket peer, since Cloudflare Tunnel traffic all arrives from one local
  address otherwise).
- `POST /api/logout` clears the session.
- `GET /api/bins` and both write endpoints require a valid session; writes
  additionally require `role == "editor"` (403 otherwise).
- Static files (`index.html`, images) stay unauthenticated so the login
  screen itself can load — the embedded fallback copy in `index.html` was
  already visible in page source regardless, by design, for the offline/USB
  read-only mode.

## Data model (`bins.json`)
Top level: `{ "version", "count", "bins": [ ...records ] }`. Each record:
- `id` — stable key: `"<cab>-<bin>"` (e.g. `"B-6-1"`); no-bin items use `"NB-<last4>"`
- `cab` — `"A"`, `"B"`, or `"?"` (back-stock item with no bin yet)
- `bin` — code within the cabinet, e.g. `"E3"`, `"6-1"` (`"—"` for no-bin)
- `row`, `col` — parsed from `bin`
- `pn` — full part number (string). Legacy no-bin items (read off a box label
  with only the last 4 visible) store `"…<last4>"`; live-added no-bin items
  store the real full number, since whoever adds them typically knows it.
- `zone` — `"Left Door"` | `"Main"` | `"Right Door"` | `"Back stock — no bin"`
- `verify` — `true` = low-confidence read, eyeball before trusting
- `boxes` — refill boxes on the shelf (int, optional) — **seed value; live count lives in state.json**
- `binFull` — cabinet bin is full (bool, optional) — seed value
- `boxNote` — note about the count (misread warning etc.)

Search matches the **last 4 digits** of `pn`. Both cabinets stock similar GM
numbers, so last-4 collisions happen — the UI shows every match with its cabinet
+ full number. Bin codes display prefixed `CA-` / `CB-`.

## Physical addressing
- **Cabinet A** — left door rows **A–C**, main body **D–L**, right door **M–T**.
  (D–L as "main" was inferred, not yet confirmed by the user.)
- **Cabinet B** — left door rows **A–I** plus a **numeric block (rows 1–6, 4 cols)**
  below the letters; main **J–R**; right door **S–Z**.

## Editing data (normal workflow)
1. Edit `public/bins.json` (add/fix a record, adjust a curated field).
2. Bump `version`.
3. Commit. Static app picks it up next load; server serves it directly.
Keep `boxes`/`binFull` here as *seed* values only — live counts are server-side.

## Going live (T320) — status: live
The server splits **curated data** (`bins.json`, tracked) from **live state**
(`state.json`, ignored):
- First run seeds `state.json` from each record's `boxes`/`binFull`.
- `GET  /api/bins` → `bins.json` with live `boxes`/`binFull` overlaid (by `id`),
  auth required (see **Auth**).
- `POST /api/box {id, delta}` → `+1` received / `-1` to bin (clamps at 0);
  initializes unseen ids to `{boxes:0, binFull:false}` first, so adding the
  first box to a bin with no prior count works. Auth + `editor` role required.
- `POST /api/binfull {id, value}` → toggle. Auth + `editor` role required.
- `POST /api/parts {pn, cab, bin, boxes}` → adds a brand-new part, either to
  a real bin (`cab` + `bin` both required; `row`/`col`/`zone` auto-computed
  from `bin` the same way the physical addressing scheme works) or as a
  no-bin back-stock item (omit `cab`/`bin`; id becomes `NB-<last4>`). Rejects
  with 409 if the target bin/no-bin-id is already taken — this never
  overwrites an existing record. Writes to `additions.json` + seeds
  `state.json` in the same transaction. Auth + `editor` role required.
Run: `python3 server/server.py` (serves `:8080`, override with `PORT=`).

Front end: every bin card (not just ones with a seeded `boxes` count) renders
`+ received` / `− to bin`; `Bin full` renders on every real cabinet bin but is
omitted for no-bin (`NB-`) back-stock entries. Buttons stay disabled until a
session is authenticated, and again if the logged-in employee's role is
`viewer`. Falls back to read-only static mode if the API isn't reachable.
A `+ Add new part` button (header, only rendered when the live API is
reachable) opens a form for logging a part that isn't in the system yet —
pick a cabinet + bin, or leave it as back-stock-only with no home. Same
editor-only gating as the box buttons; posts to `/api/parts`.

## Don'ts
- Don't use the 90-bin **CB-only** `bins.json` from the accidental side chat —
  this 238-bin (240-record) two-cabinet file is the source of truth.
- Don't hand-edit `state.json` for curated changes — those belong in `bins.json`.
- Don't hand-edit `additions.json` either — it's the live-add equivalent of
  `state.json`. If a live-added part should graduate into the curated,
  git-tracked `bins.json` (e.g. once its bin is double-checked), move the
  record over by hand and remove it from `additions.json` so it isn't
  double-counted by `merged()`.
