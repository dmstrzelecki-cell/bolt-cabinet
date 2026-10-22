# Punch list

## Deploy record
- **Current known-good SHA: `a6b81b0`** — deployed to CT 300 on 2026-10-22
  (rack/level, move+swap, last-edited). Verified live: fast-forward pull onto
  a clean tree, unit active, `/api/bins`, `/api/bins/edit` and the new
  `/api/bins/move` all return **401** unauthenticated while `/` stays public,
  `merged()` serves 246 records plus the rack definitions, and the startup
  backfill stamped **81** records from the real `audit.log` (both key
  generations). The inferred bin map accepts the known-empty CA-D4, CA-N3 and
  CB-5-1 without a confirm and flags CA-D9 and CA-Z9. No bin holds two parts
  yet. **Not verified on the container:** anything behind a login — a real
  swap, share, or rack/level write — since that needs someone's badge and PIN.
  Those paths were verified against a sandbox copy of the same commit.
- **Previous known-good: `41686c3`** — deployed 2026-08-20, verified live:
  auth gate returns 401 unauthenticated, super-user login succeeds,
  `/api/bins` serves 242 records (240 seed + 2 live additions) with 51 live
  box counts intact.
- **Previous known-good (pre-auth): `0141340`.** Roll back with
  `git -C /opt/bolt-cabinet reset --hard 0141340 && systemctl restart bolt-cabinet`
  — note that the old code needs `employees.json`, which now exists **only**
  in `/root/bolt-cabinet-preauth-20260821T023904Z/` on the container.
- Pre-migration snapshot of all live data (state, additions, audit,
  employees) is in that same `/root/bolt-cabinet-preauth-*/` directory,
  checksum-verified at the time it was taken.
- Daily backup timer `bolt-cabinet-backup.timer` is installed and enabled.

## Open decisions (need David)
- **`verify: true` after an edit.** An `edit_bins` correction currently
  leaves the `Verify` flag alone, so a bin someone just stood in front of and
  confirmed still shows "Verify". Clearing it on edit was not specified, so
  it was not done. Worth deciding — most of the low-confidence list below
  would clear itself through normal use. A **move** or **swap** leaves it
  alone for the same reason.
- **Rack labels are literally "Rack 1" and "Rack 2".** Confirmed on
  2026-10-22 as what to ship. If the techs call them something else, it is a
  one-line change to `BACKSTOCK_RACKS` in `server/server.py` — but any
  rack name already stored in `overrides.json` would then stop matching and
  those records would list under *On an unrecognised rack* until re-set.
- **Freed bin, taken id.** Record ids are `<cab>-<bin>` and are never
  re-keyed, so if a part is edited *out* of CA-L3, the physical bin is free
  but a new part can't be added there — the id `A-L3` is still taken by the
  moved record. Rare, and 409s cleanly with an explanatory message rather
  than corrupting anything. Fix would be an id suffix scheme; not invented
  here.

## Resolved
- **Shared-IP lockout no longer strands the shop.** Everyone reaches the app
  through one Cloudflare-forwarded address, so five bad attempts used to lock
  out every user — including the only people who could clear it. A super-user
  with correct credentials now logs in *through* an IP lock and clears it for
  everyone; `manage_users` holders get an **Unlock** button per user, and
  `adminctl.py unlock <badge>` works from the console. A super-user's own
  badge lock still applies, so the admin badge is not a brute-force free pass.

## Known limitations (deliberate, revisit later)
- **A shared bin and the `<cab>-<bin>` id scheme.** Two parts can now
  deliberately share a bin (**Change bin → Put both here**). The ids stay
  what they always were, so a shared bin has two records whose ids name
  different bins, and `/api/bins/add` into that bin still 409s on the id even
  though the bin itself accepts another part. Sharing is rare and the 409 is
  explanatory, so this is left alone — the fix is the same id-suffix scheme
  the *freed bin, taken id* item below would need.
- **A swap leaves ids pointing at the wrong bins.** After swapping CA-E1 and
  CA-E2, record `A-E1` is in bin E2. Deliberate: ids are keys that
  `state.json` counts hang off, and re-keying would orphan a box count. It
  does mean an id in `audit.log` is not a location.
- **`lastEdited` only goes back as far as `audit.log`.** The backfill at
  startup reads both log generations (`employeeId`/`binId` and
  `user_id`/`target_id`), but a record never touched since the log began
  shows `Last edited —`. It fills gaps only; a live stamp always wins.
- **No self-service PIN change.** The PIN an admin types when creating someone
  is theirs permanently until an admin resets it. So new PINs get read aloud,
  and nobody can rotate their own if it's overheard or shoulder-surfed.
  Deferred on 2026-08-20, not a blocker. The fix would be a
  `POST /api/me/pin` taking current + new PIN — no permission flag needed,
  since it only touches the caller's own record — plus a field in the app.

## Carried-forward risks
- **Git credentials for the container.** Nobody has written down what happens
  when the read-only deploy key at `/opt/bolt-cabinet/.git_deploy_key`
  expires or is rotated. The pull works today because of how the remote is
  configured; that is not documented anywhere.
- **No HTTPS between Cloudflare and the origin.** Plain HTTP over the LAN.
  Acceptable given the tunnel, but PINs now cross that hop.
- **PIN auth on a public endpoint is modest security.** Appropriate for
  bolt-bin data. Do not extend this auth to anything more sensitive without
  a rethink.
- **`employees.json` is retired.** The old scheme used the badge number *as*
  the PIN, which the new rules reject outright, so there was no migration
  path — every user is created fresh via `adminctl`. Delete the old file from
  the container once the new roster is in.

## Data: missing / empty bin positions (need a part # or confirm empty)
- CB left-door numeric **5-1** (opened up when 6-1 was corrected to 11610124)
- CA main **D2–D5**, **I2–I4**, **J4–J5**
- CA right door **N3**

## Data: reshoot
- CB numeric **rows 5 & 6** — one clean straight-on shot. Row-5 reads are
  suspect (5-2 …8744 vs confirmed 6-2 …8734). Rows 1–4 are fine.

## Data: back-stock shelf locations (need a walk-round)
- Every record with boxes starts with **no rack/level**, so the first
  **back stock** list after this deploy will show all of them under
  *Needs a shelf location*. Setting them is a one-time pass with the two
  dropdowns on each card — rack, then level.

## Data: back stock with no home (need full part # + a bin)
- ~~**…5044** — 8 boxes, no match anywhere.~~ **Closed 2026-10-22.** It was
  11605044 all along: someone had already added the real record at **CB-O8**,
  so the placeholder `NB-5044` was a phantom holding the same stock twice.
  CB-O8 now carries the shelf location (Rack 1 · Top) and the counted 7
  boxes, and `NB-5044` has been removed from the seed.

## Data: row-width unknowns (is there one more bin on the end?)
- CA main **E8?**

## Data: low-confidence reads (`verify: true`) to confirm
- CA: A4 "2275", C4–C8 layout, F1/K1 (dup 11603036), F5 "11605396", H6,
  I1, L3 "6583", M5 "1X530375A", S4 "9413349"
- CB numeric 1-1…4-4 and 5-2/5-3/5-4
- CA H1–H4 are sandpaper/foam (not fasteners) — confirm they stay unlisted

## Data: confirm
- Cabinet A **D–L = main body** (currently inferred).
