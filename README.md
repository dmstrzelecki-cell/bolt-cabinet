# Bolt Cabinet Lookup

Last-4 part-number → bin-location lookup for the shop fastener cabinets
(Cabinet A + Cabinet B), with back-stock box tracking.

## Run it
- **Standalone / offline:** open `public/index.html` in any browser (works off a
  USB stick). Read-only — shows box counts but the +/− buttons are inactive.
- **On the T320 (live box counts):** `python3 server/server.py`, then browse to
  `http://<t320-ip>:8080` and log in with your 6-digit employee PIN. Needs
  `employees.json` present next to `state.json` (see **CLAUDE.md → Auth**) —
  without it, no PIN will be accepted.

## Update inventory
Edit `public/bins.json` (one record per bin) and bump `version`.
See **CLAUDE.md** for the record schema + addressing scheme, and **PUNCHLIST.md**
for what's still open before go-live.

## Search tips
- Type the **last 4 digits** of a part number.
- Type **back stock** to list every refill-box item (no-bin ones grouped on top).
