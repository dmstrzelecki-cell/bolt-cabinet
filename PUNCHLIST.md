# Punch list

## Deploy record
- **Known-good SHA before the auth/editing work:** `0141340`
  (`Show numeric keypad on mobile for part number search`).
  Roll back with `git -C /opt/bolt-cabinet reset --hard 0141340 && systemctl restart bolt-cabinet`.
- Record the new known-good SHA here once §9.3 verification passes.

## Open decisions (need David)
- **`verify: true` after an edit.** An `edit_bins` correction currently
  leaves the `Verify` flag alone, so a bin someone just stood in front of and
  confirmed still shows "Verify". Clearing it on edit was not specified, so
  it was not done. Worth deciding — most of the low-confidence list below
  would clear itself through normal use.
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

## Data: back stock with no home (need full part # + a bin)
- **…5044** — 8 boxes, no match anywhere.
  *(Now fixable in the app: search it, hit **Edit**, set the full part number
  and a bin. The record keeps its box count.)*

## Data: row-width unknowns (is there one more bin on the end?)
- CA main **E8?**

## Data: low-confidence reads (`verify: true`) to confirm
- CA: A4 "2275", C4–C8 layout, F1/K1 (dup 11603036), F5 "11605396", H6,
  I1, L3 "6583", M5 "1X530375A", S4 "9413349"
- CB numeric 1-1…4-4 and 5-2/5-3/5-4
- CA H1–H4 are sandpaper/foam (not fasteners) — confirm they stay unlisted

## Data: confirm
- Cabinet A **D–L = main body** (currently inferred).
