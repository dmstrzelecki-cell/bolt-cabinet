# Family Search

A private family search engine for **search.thestrzs.net**. It searches only
parent-approved websites, a parent-curated library, and chosen Immich photo
albums, behind Cloudflare Access. It's built for a phone with Family Link set
to "Only allow approved sites". Design and rules: [CLAUDE.md](CLAUDE.md).
Status: [TEST-RESULTS.md](TEST-RESULTS.md). Preflight: [PREFLIGHT.md](PREFLIGHT.md).

```
Phone (Family Link) → Cloudflare Access → existing tunnel → CT familysearch:8080 (FastAPI)
                                                             ├─ Meilisearch 127.0.0.1:7700
                                                             ├─ crawler (own user, approved hosts only)
                                                             └─ CT101 Immich :2283 (restricted user)
```

## Deploy runbook

### 1. Preflight (read-only, on the Proxmox host)
```bash
bash scripts/preflight.sh > /root/PREFLIGHT.md
```
Read it. **Stop:** David approves the CTID, IP, storage and hostname.

### 2. Create the CT (after approval, on the host)
```bash
bash scripts/create-ct.sh --ctid <id> --ip 192.168.0.<n> --storage <rootfs-storage> [--ssd <ssd-storage>]   # dry run
bash scripts/create-ct.sh ... --yes
```
That gives you an unprivileged Debian 12 CT: 4 vCPU, 8 GB RAM, 80 GB disk, hostname
`familysearch`, DNS 192.168.0.40, `nesting=1`. With `--ssd` it also adds a 40 GB
mount point for Meilisearch on that storage, because the main pool is SMR.

### 3. Copy the code in and install (one paste)
From a machine with this repo:
```bash
git -C bolt-cabinet archive --format=tar.gz --prefix=family-search-src/ HEAD:family-search > family-search.tgz
scp family-search.tgz root@<proxmox-host>:/root/
```
On the host:
```bash
pct push <CTID> /root/family-search.tgz /root/family-search.tgz
pct exec <CTID> -- bash -c 'cd /root && tar -xzf family-search.tgz && bash /root/family-search-src/scripts/install.sh'
```
`install.sh` is safe to run again. It shows a step counter and progress
bars, creates the service users, syncs the code to `/opt/family-search`, and
seeds the live config only where a file is missing. It also generates the secrets
(never printed), verifies the Meilisearch binary's sha256, installs the
hash-pinned Python packages, sets up the firewall, starts everything, and then
lists the placeholders still left to fill.

**Updates** are the same three commands with a new tarball. Live config, data
and secrets are never overwritten.

### 4. Fill the placeholders, then re-run `install.sh`
| File (inside the CT) | Key | Where it comes from |
|---|---|---|
| `/etc/family-search/site.env` | `CLOUDFLARED_IP` | PREFLIGHT "Cloudflare Tunnel": the LAN IP running cloudflared. Until it's set, port 8080 is closed to the LAN. |
| `/etc/family-search/site.env` | `IMMICH_IP` | CT101's IP from PREFLIGHT |
| `/etc/family-search/site.env` | `ADMIN_SSH_SOURCE` | optional. Until it's set there's no SSH at all; `pct enter` still works. |
| `/etc/family-search/backend.env` | `CF_ACCESS_TEAM_DOMAIN` | e.g. `yourteam.cloudflareaccess.com` (Cloudflare step below) |
| `/etc/family-search/backend.env` | `CF_ACCESS_AUD` | the Access application's Audience (AUD) tag |
| `/etc/family-search/backend.env` | `PARENT_EMAILS` | `david@…, wife@…`, comma-separated |
| `/etc/family-search/backend.env` | `ALLOWED_EMAILS` | optional extra guard: the child's login email |
| `/etc/family-search/backend.env` | `IMMICH_URL` | `http://<CT101 IP>:2283` |
| `/etc/family-search/backend.env` | `IMMICH_API_KEY` | the restricted key (Immich step below) |

Edit these with `nano` inside the CT. Never paste them into chat or commit them.
Until the Access values are set, the app answers **503** to everything (it
fails closed).

### 5. Verify on the CT
```bash
bash /opt/family-search/scripts/verify.sh
```
From another LAN host, `curl -m 5 http://<CT-IP>:8080/` must **time out**.
Then `pct reboot <CTID>` and run `verify.sh` again.

## David's manual steps (after local tests pass)
**Immich:**
- [ ] Create the non-admin user `family-search`.
- [ ] Share only the approved albums with it, as a viewer.
- [ ] Create a minimum-scope API key: `album.read`, `asset.view`, plus `asset.read` only if `check-immich` says the album endpoint omits assets. Grant nothing more.
- [ ] Put it in `backend.env`, then `systemctl restart family-search`, and run `scripts/as-service.sh web /opt/family-search/venv/bin/python -m familysearch.cli check-immich`.
- [ ] Add the album IDs on `/parent` → Photos, then press **Sync albums now**.

**Cloudflare:**
- [ ] Route `search.thestrzs.net` through the existing tunnel to `http://<CT-IP>:8080`.
- [ ] Create a self-hosted Access application for that hostname and enable One-time PIN.
- [ ] Add an Allow policy for the exact parent emails plus a dedicated child login email that a parent controls.
- [ ] Set the session duration to 1 month to start.
- [ ] Copy the team domain and the AUD tag into `backend.env`.
- [ ] Confirm the Access login page actually appears. Other hostnames have had Access Bypass issues.

**Family Link:**
- [ ] Keep Chrome on "Only allow approved sites".
- [ ] Approve `search.thestrzs.net` and `yourteam.cloudflareaccess.com`.
- [ ] Approve every host on `/parent` → **Family Link**. That tab has a copy-ready list and a checkbox per host, so new hosts stand out.
- [ ] Block other browsers on her phone.
- [ ] Sign in on her phone, enter the one-time code, and use **Add to Home screen**.

## Operating it
- **/parent** (parents only) covers requests, sites (add/edit/disable/remove, crawl now, status), the Family Link list, library (links, notes, PDFs up to 25 MB), photo albums and sync, blocked words and synonyms, home-page tiles, the search-history toggle (off by default), and the audit log (parent changes, crawls, syncs, denied access).
- **Crawls** run nightly from 01:10. Each host refreshes weekly on its own night. The first crawl is spread over several nights (4 h budget per night). `simple.wikipedia.org` is imported from the official Wikimedia dump, checksum-verified, and its links point to the live pages.
- **Backups** run nightly to `/var/backups/family-search` and keep the last 14. Restore with `scripts/restore.sh <file>`, adding `--with-secrets` to restore the env files too.
- **Rebuild the index** with `scripts/rebuild-index.sh`. Nothing is lost, because the index is only a cache.
- **Logs:** `journalctl -u family-search -u family-crawler -u meilisearch`. Queries are never logged.

## Development
```bash
python3 -m venv .venv && .venv/bin/pip install -r <(sed 's/ --hash.*//' backend/requirements.lock) pytest
.venv/bin/pytest tests/                      # 63 tests, fakes for Meilisearch / Immich / the web
PYTHON=.venv/bin/python tests/e2e/run.sh     # Chromium at 360 px (needs Node + Playwright)
```
