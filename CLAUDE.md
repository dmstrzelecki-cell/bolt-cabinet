# Bolt Cabinet Lookup — project context

Fast **last-4 part-number → bin-location** lookup for the Penske fastener
cabinets. Runs today as a static single-file page; deploys to the shop **T320**
as a small stdlib Python server that also persists back-stock box counts.

## Layout
- `public/` — what gets served
  - `index.html` — single-file app (search UI). Loads `bins.json` at runtime and
    falls back to an embedded copy if the fetch fails, so it also works opened
    straight off disk/USB (read-only in that mode).
  - `bins.json` — **the data master. Edit this to change inventory.** One record per bin.
  - `images/` — optional fastener photos named `<partnumber>.jpg` (`.png`/`.webp`
    also work); auto-shown when a part is found.
- `server/server.py` — T320 deploy server (stdlib only). Serves `public/` + a
  small JSON API for box counts. See **Going live**.
- `state.json` — live box counts, created & owned by the server. **Gitignored.**
- `PUNCHLIST.md` — open data gaps to close before go-live.

## Data model (`bins.json`)
Top level: `{ "version", "count", "bins": [ ...records ] }`. Each record:
- `id` — stable key: `"<cab>-<bin>"` (e.g. `"B-6-1"`); no-bin items use `"NB-<last4>"`
- `cab` — `"A"`, `"B"`, or `"?"` (back-stock item with no bin yet)
- `bin` — code within the cabinet, e.g. `"E3"`, `"6-1"` (`"—"` for no-bin)
- `row`, `col` — parsed from `bin`
- `pn` — full part number (string). No-bin items store `"…<last4>"`.
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

## Going live (T320) — remaining work
The server splits **curated data** (`bins.json`, tracked) from **live state**
(`state.json`, ignored):
- First run seeds `state.json` from each record's `boxes`/`binFull`.
- `GET  /api/bins` → `bins.json` with live `boxes`/`binFull` overlaid (by `id`).
- `POST /api/box {id, delta}` → `+1` received / `-1` to bin (clamps at 0).
- `POST /api/binfull {id, value}` → toggle.
Run: `python3 server/server.py` (serves `:8080`, override with `PORT=`).

**One front-end task makes it interactive** (deferred to deploy on purpose):
in `index.html`, point the data fetch at `/api/bins`; when it responds, enable
the `+ received` / `− to bin` / `Bin full` buttons (currently rendered disabled)
and wire them to the POST endpoints, refreshing the count from the response.
Fall back to read-only static mode if `/api/bins` isn't reachable.

## Don'ts
- Don't use the 90-bin **CB-only** `bins.json` from the accidental side chat —
  this 238-bin (240-record) two-cabinet file is the source of truth.
- Don't hand-edit `state.json` for curated changes — those belong in `bins.json`.
