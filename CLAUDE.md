# Bolt Cabinet Lookup — project context

Fast **last-4 part-number → bin-location** lookup for the Penske fastener
cabinets. Runs today as a static single-file page; deploys to the shop **T320**
(a Proxmox LXC container, `boltsapp`, `192.168.0.126`) as a small stdlib Python
server that persists back-stock box counts, lets techs correct bin
locations in place, and gates everything behind a per-employee badge + PIN
login with role-based permissions.

## Layout
- `public/` — what gets served
  - `index.html` — single-file app: search UI, badge+PIN login, edit modal,
    admin panel. Tries `/api/me` then `/api/bins` at runtime; falls back to
    static `bins.json`, then an embedded copy, so it also works opened
    straight off disk/USB (read-only in that mode, no login prompt, since a
    login could never succeed there — see **Auth**).
  - `bins.json` — **the tracked seed.** One record per bin. The server never
    writes to it; see **Editing data**.
  - `images/` — optional fastener photos named `<partnumber>.jpg` (`.png`/`.webp`
    also work); auto-shown when a part is found.
- `server/server.py` — T320 deploy server (stdlib only). Serves `public/` +
  the JSON API. See **Auth**, **Endpoints**, **Going live**.
- `server/adminctl.py` — admin CLI: bootstrap, users, PIN resets, backups,
  restore, export. Imports `server.py`; never duplicates its logic.
- `server/unit/` — the daily-backup systemd timer. The app's own
  `bolt-cabinet.service` is **not** tracked here.
- `PUNCHLIST.md` — open data gaps and carried-forward risks.

**Server-owned, gitignored, live in `/opt/bolt-cabinet/` — never commit:**

| File | Contents |
|---|---|
| `.env` | `BOLT_SESSION_KEY`. Generated on the box, mode 600. |
| `users.json` | Badge → name, scrypt PIN hash, permission flags. Mode 600. |
| `state.json` | Live box counts / `binFull`, keyed by record id. |
| `overrides.json` | Bin/cabinet/part-number edits overlaying `bins.json`. |
| `additions.json` | Brand-new records added from the app. |
| `audit.log` | JSONL, one line per change: who, what, before/after, ts. |
| `backups/` | Timestamped snapshots of all of the above. Last 14 kept. |

## Deploy topology
- Code lives at `github.com/dmstrzelecki-cell/bolt-cabinet` (private). Push
  from a dev machine with the write-access deploy key; the T320 container
  pulls with a separate **read-only** deploy key
  (`/opt/bolt-cabinet/.git_deploy_key`, owned by `boltapp`, excluded via
  `.git/info/exclude` — not the tracked `.gitignore`, since it's container-local).
- To ship an update, see **Deploying an update** below.
- Runs as `systemd` unit `bolt-cabinet`, under unprivileged user `boltapp`,
  enabled on boot. Reachable on the shop LAN at `192.168.0.126:8080` and
  remotely through a **Cloudflare Tunnel**, which publishes
  `boltbins.thestrzs.net` without a router port-forward and terminates HTTPS.
  The tunnel stays. The Cloudflare **Access** policy (the old email gate) is
  being removed now that the app owns its own auth — that removal is a manual
  dashboard step, never scripted from here.
- Because the origin is also reachable on the flat LAN by any host on
  `192.168.0.0/24`, bypassing Cloudflare entirely, auth is enforced by the
  app itself and never by anything upstream. No proxy header is trusted for
  identity.

## Auth
The app owns its own auth; it is not behind an email gate. Assume the login
page is the only wall, because once the Cloudflare **Access** policy is off
`boltbins.thestrzs.net` is reachable by anyone. (The **Tunnel** stays — that
is what publishes the hostname and terminates HTTPS.)

Every user has a **badge number + PIN** in `users.json` (gitignored,
container-only). PINs are `scrypt`-hashed with a per-user salt and are never
stored, logged, or returned by any endpoint.

```json
{"version":1,"users":[{"id":"1047","name":"David S.",
  "pin":{"algo":"scrypt","salt":"…","hash":"…","n":16384,"r":8,"p":1},
  "perms":["manage_users","edit_bins","add_parts","adjust_counts","toggle_binfull","view"],
  "active":true,"created":"…","last_login":null,"failed":0,"locked_until":null}]}
```

- `id` is the badge number: digits, unique, **immutable once created**.
- Retire someone with `active:false`, not deletion — that keeps `audit.log`
  attribution resolvable. Hard delete exists but should be rare.

### Permissions
Six flags, defined in **one** place — `PERMS` in `server/server.py`. Changing
the set is an edit there plus `ROUTE_PERMS` and the UI's chip labels.

| Flag | Grants |
|---|---|
| `view` | log in, search, see bins and counts |
| `adjust_counts` | `+ received` / `− to bin` |
| `toggle_binfull` | the Bin-full toggle |
| `edit_bins` | change bin / cabinet / part number on an existing entry |
| `add_parts` | create a new entry, including `NB-<last4>` back stock |
| `manage_users` | add/deactivate users, set permissions, reset PINs, clear lockouts |

A **super-user** is simply a holder of `manage_users`. It implies nothing
else — grant every flag explicitly.

### Sessions and lockout
- `POST /api/login {id, pin}` → HMAC-SHA256-signed cookie carrying only a
  user id and issue time. `HttpOnly; SameSite=Lax; Secure; Path=/`, 12h TTL.
  The signature is verified before any field in it is trusted.
- Sessions are **stateless**. Permissions are re-read from `users.json` on
  every request, so a restart doesn't sign everyone out and revoking a flag
  or deactivating an account takes effect on the very next request.
- `Secure` is set unconditionally, so **login only works over HTTPS** —
  i.e. through the tunnel hostname. Browsing to `http://192.168.0.126:8080`
  by IP serves the page but cannot complete a login. This is deliberate.
- 5 failed attempts locks that badge for 15 minutes, persisted in
  `users.json` so a restart can't clear it; a parallel in-memory counter
  locks the source IP. The rate-limit key prefers `CF-Connecting-IP`, since
  tunnel traffic otherwise all arrives from one local address.
- **A super-user is never shut out by an IP lockout.** The whole shop shares
  one Cloudflare-forwarded address, so one person fumbling five times would
  otherwise lock out everybody — including the only people who could clear
  it. Correct credentials + `manage_users` + an unlocked badge gets through
  an IP lock, and **succeeding clears that lock for everyone**.
- A super-user's *own badge* lockout still applies — no free pass for
  brute-forcing the admin badge. Clear it from the console with
  `adminctl.py unlock <badge>`.
- Failures that arrive while an IP is already locked re-arm the IP lock but
  are deliberately **not** counted against the badge, so an attacker can't
  lock the shared IP and then walk every badge in the shop into a lockout.
- `POST /api/admin/users {action:"unlock", id}` clears a badge lockout
  without touching the PIN, and drops IP locks at the same time. The admin
  panel shows a `LOCKED` pill and an Unlock button on affected rows.
- Login returns **one identical message and status** for unknown badge,
  wrong PIN, locked, and deactivated. Do not add a reason, a `retryAfter`,
  or a distinct status code — that turns the endpoint into a badge oracle.
- `BOLT_SESSION_KEY` lives in a gitignored `.env` generated on the
  container. The server refuses to start without it. Never commit, print,
  or paste it.
- Static files stay unauthenticated so the login screen can load. The
  embedded fallback copy in `index.html` was already public in page source
  by design, for the offline/USB read-only mode.

## Endpoints
| Method | Path | Perm |
|---|---|---|
| POST | `/api/login` | — |
| POST | `/api/logout` | session |
| GET | `/api/me` | session (own name + own flags only) |
| GET | `/api/bins` | `view` |
| POST | `/api/box` | `adjust_counts` |
| POST | `/api/binfull` | `toggle_binfull` |
| POST | `/api/bins/edit` | `edit_bins` |
| POST | `/api/bins/add` | `add_parts` (`/api/parts` is a legacy alias) |
| GET/POST | `/api/admin/users` | `manage_users` |
| POST | `/api/admin/users/pin` | `manage_users` |

`_require(path)` is the single gate. A route with no `ROUTE_PERMS` entry is
refused, so a new endpoint can't accidentally default open. Hiding a button
in the UI is cosmetic — the server re-checks on every request.

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

## Editing data
`public/bins.json` is a **git-tracked seed the server never writes to.** That
is not a style preference: `git pull --ff-only` fails on a dirty tree, so if
the server wrote to a tracked file, the first tech to correct a bin location
would block every future deploy until someone resolved a conflict from a
phone. Three layers instead, applied in this order by `merged()`:

    bins.json  ->  overrides.json  ->  state.json counts

- **From the app** (normal): `edit_bins` fixes an existing entry, `add_parts`
  creates a new one. These write `overrides.json` / `additions.json`.
- **By hand** (bulk or structural): edit `public/bins.json`, bump `version`,
  commit. Keep `boxes`/`binFull` here as *seed* values only.
- **Reconciling**: run `adminctl.py export` occasionally to fold overrides
  back into the seed, review the diff on the dev machine, commit
  deliberately. It never commits, pushes, or clears `overrides.json` itself.

An edit that changes `cab`/`bin` **does not re-key the record id.** The id
stays its original `<cab>-<bin>` value forever — it is only an identifier,
and both `state.json` counts and the override map hang off it. Re-keying
would orphan a bin's box count.

## Going live (T320) — status: live
Run: `python3 server/server.py` (serves `:8080`, override with `PORT=`).
The server refuses to start without `BOLT_SESSION_KEY` in `../.env`.

Write endpoints, all gated per the **Endpoints** table:
- `POST /api/box {id, delta}` → `+1` received / `−1` to bin (clamps at 0);
  initializes unseen ids to `{boxes:0, binFull:false}` first, so adding the
  first box to a bin with no prior count works.
- `POST /api/binfull {id, value}` → toggle.
- `POST /api/bins/add {pn, cab, bin, boxes}` → a brand-new part, either in a
  real bin (`cab` + `bin` both required; `row`/`col`/`zone` derived from
  `bin`) or as back stock with no home (omit both; id becomes
  `NB-<last4>`). 409 if the bin is already occupied — never overwrites.
  Writes `additions.json` + seeds `state.json` in one transaction.
- `POST /api/bins/edit {id, cab?, bin?, pn?, note?}` → corrects an existing
  entry. `row`/`col`/`zone` are always recomputed server-side and never
  taken from the client. 409 if the target bin is occupied, naming the part
  in the way. Writes `overrides.json`.

Front end: every bin card renders `+ received` / `− to bin`; `Bin full`
renders on real cabinet bins but not on `NB-` back-stock entries; `Edit`
renders for `edit_bins` holders. The header shows `+ Add new part` and
`Manage users` only to holders of `add_parts` / `manage_users`. Every
control is gated on the caller's own flags from `/api/me` — cosmetically;
the server re-checks. Any `401` returns to the login screen while keeping
the typed search. With no API reachable at all the app falls back to the
static read-only view rather than an unusable login screen (USB/offline).

## Deploying an update
1. Push from the dev machine.
2. On the T320: `su boltapp -s /bin/bash -c 'cd /opt/bolt-cabinet && git pull --ff-only'`
3. Restart **only if `server.py` changed** — data and front-end changes are
   picked up live, since the server reads files fresh every request:
   `systemctl restart bolt-cabinet`

If the pull fails on a dirty tree, **do not `reset --hard` blindly.** Report
what is dirty first — a modified tracked file in `/opt/bolt-cabinet` means
something wrote where it shouldn't, and that is the bug to fix.

Verifying auth is live: `curl` against `http://localhost:8080/api/bins` from
inside the container should return **401**. That is success, not a
regression. A `200` there means the gate is not working.

Rollback: `git -C /opt/bolt-cabinet reset --hard <previous-sha>` then restart
the unit. Data rollback is `adminctl.py restore <timestamp>`.

## Backups
`users.json`, `state.json`, `overrides.json` and `additions.json` are live
data with **no git safety net**. `adminctl.py backup` snapshots all four plus
`audit.log` into `backups/<iso>/`, keeping the last 14. It runs from a daily
systemd timer (`server/unit/`) and automatically before any destructive
admin action — user deactivation or deletion, PIN reset, restore.

## Don'ts
- Don't use the 90-bin **CB-only** `bins.json` from the accidental side chat —
  this 238-bin (240-record) two-cabinet file is the source of truth.
- Don't let the server write to `public/bins.json`. See **Editing data** —
  a dirty tracked file breaks every future `git pull --ff-only`.
- Don't hand-edit `state.json`, `overrides.json`, or `additions.json`.
  Curated changes belong in `bins.json`; use `adminctl.py export` to
  reconcile. If a live-added part should graduate into the tracked seed,
  export rather than copying records by hand.
- Don't re-key a record id when its cab/bin changes. See **Editing data**.
- Don't add a reason, `retryAfter`, or a distinct status code to a failed
  login. See **Auth**.
- Don't put a PIN in an `adminctl.py` argument — it lands in shell history.
  Every PIN path prompts with no echo.
- Don't commit `users.json`, `overrides.json`, `state.json`, `additions.json`,
  `audit.log`, `.env`, or `backups/`. Don't put a secret in a commit message
  or a doc file.
- Don't touch Cloudflare from here. The **Access policy** is David's manual
  dashboard step; the **Tunnel** stays. `cloudflared` is not installed in the
  container and must not be.
- Don't add a dependency, a build step, or a framework. `server.py` and
  `adminctl.py` are stdlib-only; `index.html` stays a single file.
