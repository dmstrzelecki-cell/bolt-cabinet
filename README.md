# Bolt Cabinet Lookup

Last-4 part-number → bin-location lookup for the shop fastener cabinets
(Cabinet A + Cabinet B), with back-stock box tracking and in-app correction
of bin locations.

## Using it

Go to **https://boltbins.thestrzs.net** and log in with your **badge number**
and **PIN**.

What you can do depends on the permissions you were given:

| You can | If you have |
|---|---|
| Search and see counts | `view` |
| `+ received` / `− to bin` | `adjust_counts` |
| The Bin-full toggle | `toggle_binfull` |
| **Edit** a bin location, part number or shelf location | `edit_bins` |
| **Change bin** — move, swap or share a bin | `edit_bins` |
| **+ Add new part** | `add_parts` |
| **Manage users**, unlock people | `manage_users` |

Controls you don't have simply aren't shown. Sessions last 12 hours; after
that you'll be asked to log in again and your search is kept.

**Five wrong tries locks your badge for 15 minutes.** The message is the same
whatever went wrong, on purpose. Ask someone with `manage_users` to hit
**Unlock** next to your name — that clears the lock without changing your PIN.

Because the whole shop shares one address, a run of bad attempts can lock
*everyone* out for 15 minutes. A super-user is exempt: log in normally and
that clears the lock for the rest of the shop as well. If your own badge is
also locked, clear it from the container console:

```bash
python3 /opt/bolt-cabinet/server/adminctl.py unlock <badge>
```

> Login requires HTTPS, so use the address above. Browsing directly to
> `http://192.168.0.126:8080` on the shop LAN will show the page but cannot
> log you in.

### Search tips
- Type the **last 4 digits** of a part number.
- Type a **bin code** — `M8`, `CB-M8`, `6-1` — to list everything in that
  bin. This is how you check a bin that holds more than one part.
- Type **back stock** to walk the refill shelves: anything with boxes but no
  shelf location first, then rack by rack, top shelf down.

On a phone the bin location is shown directly under the search box and the
keyboard drops away as soon as the 4th digit resolves, so the answer isn't
hidden behind it. Tap the box again and the old digits are selected — just
type the next four over them.

### Back-stock racks

Refill boxes live on two racks. Each entry can record which rack and which
shelf, set from the two dropdowns on the result card (needs `edit_bins`):

| Location | Levels |
|---|---|
| Rack 1 | Top, Second, Third, Bottom |
| Rack 2 | Top, Middle, Bottom |
| Cabinet A Floor | — it's one spot, so there's no second dropdown |
| Cabinet B Floor | — same |

Anything with boxes but no shelf location is tagged **Location?** and sorts
to the top of the **back stock** list.

### Moving a part to another bin

**Change bin** on the result card. If the bin you name is already taken, the
app says which part is in it and offers:

- **Swap them** — the two parts exchange bins in one go.
- **Put both here** — both parts share the bin, and every search result for
  that bin then carries a *2 parts assigned to…* warning.
- **Cancel**.

If the bin code isn't one the app recognises for that cabinet it asks you to
confirm before using it, rather than refusing — so a genuinely new bin still
works, but a typo gets caught.

### Who changed what

Every result card carries a **Last edited** line — when, who, and what they
did — in shop time. It covers counts, bin-full, edits, moves and swaps.

### Offline / USB
Opening `public/index.html` straight off a USB stick still works, read-only:
no login, no buttons, counts as of whenever that copy was made.

## Running it

- **On the container:** `python3 server/server.py` (port 8080, `PORT=`
  overrides). It refuses to start without `BOLT_SESSION_KEY` — see below.
- Normally it runs as the `bolt-cabinet` systemd unit, on boot.

## Admin

All of this runs on the container, from the Proxmox console or `pct enter`:

```
python3 server/adminctl.py --help
```

### First-time setup

Generate the session key **on the box**. Do not run this anywhere else, do
not print the value, and never commit the file:

```bash
python3 -c 'import secrets;print("BOLT_SESSION_KEY="+secrets.token_urlsafe(48))' > /opt/bolt-cabinet/.env
```

Then lock it down and create the first super-user:

```bash
chmod 600 /opt/bolt-cabinet/.env && chown boltapp /opt/bolt-cabinet/.env
```

```bash
python3 /opt/bolt-cabinet/server/adminctl.py bootstrap
```

`bootstrap` prompts for badge number, name and PIN (no echo) and grants every
permission. It refuses to run once any user exists.

### Adding a coworker

```bash
python3 /opt/bolt-cabinet/server/adminctl.py add-user
```

Prompts for badge, name, permissions and a PIN. You can also do this from the
app's **Manage users** panel. PINs must be 6+ digits and can't be all one
digit, a run like `123456`, or the person's own badge number.

To change someone's access later: `set-perms <badge>`, or the same panel.
Prefer `deactivate <badge>` over deleting — it keeps their name resolvable in
the audit log.

### Backups and rollback

Snapshots of `users.json`, `state.json`, `overrides.json`, `additions.json`
and `audit.log` land in `backups/<timestamp>/`. The last 14 are kept. They
run daily from a systemd timer (install it once — see `server/unit/README.md`)
and automatically before any destructive admin action.

```bash
python3 /opt/bolt-cabinet/server/adminctl.py list-backups
```

```bash
python3 /opt/bolt-cabinet/server/adminctl.py restore <timestamp>
```

`restore` snapshots the current files first, stops the service, copies the
files back, and starts it again.

To roll back **code** rather than data, reset to the previous commit and
restart. The known-good SHA is recorded in `PUNCHLIST.md` at deploy time:

```bash
git -C /opt/bolt-cabinet reset --hard <previous-sha> && systemctl restart bolt-cabinet
```

## Updating inventory

Day to day, techs fix entries in the app — those land in `overrides.json`,
which overlays the tracked `public/bins.json` at serve time. The seed file
itself is never written to by the server, so `git pull` can't start failing
on a dirty tree.

Run this occasionally so the tracked seed doesn't drift stale:

```bash
python3 /opt/bolt-cabinet/server/adminctl.py export
```

It rewrites `public/bins.json` with the overrides folded in and bumps
`version`. It does **not** commit, push, or clear `overrides.json` — review
the diff on the dev machine and commit deliberately. Add `--with-additions`
to fold in parts that were added live, too.

For bulk or structural changes, edit `public/bins.json` by hand, bump
`version`, and commit. See **CLAUDE.md** for the record schema, the
addressing scheme, and the permission model.

## Deploying

```bash
git push
```

Then on the T320:

```bash
su boltapp -s /bin/bash -c 'cd /opt/bolt-cabinet && git pull --ff-only'
```

Restart only when `server.py` or `adminctl.py` changed — data and front-end
changes are picked up live:

```bash
systemctl restart bolt-cabinet
```

Sanity check from inside the container. **401 is the correct answer** — it
means the auth gate is working. There is no `curl` in CT 300, hence `python3`:

```bash
python3 -c "import urllib.request,urllib.error;
try: print(urllib.request.urlopen('http://localhost:8080/api/bins',timeout=5).status)
except urllib.error.HTTPError as e: print(e.code)"
```
