# PREFLIGHT — not yet run

**Status: pending.** This app was built in a cloud session that has no access
to the T320, so none of the read-only checks below have been run yet, and **no
container has been created**. The brief's stop gate stands: David approves the
CTID, IP, storage and hostname before anything is created.

## How to run it (read-only, on the Proxmox host)
```bash
bash scripts/preflight.sh > /root/PREFLIGHT.md   # changes nothing; markdown on stdout
```
Replace this file with that output (or paste it below).

## What it checks (from the brief)
| Check | How `preflight.sh` does it |
|---|---|
| Containers, IPs, RAM, storage pools, free space | `pct list`, `qm list`, `pct config` per CT, `pvesm status`, `zpool list` |
| Actual host RAM (notes say both 32 GB and 96 GB) | `free -g` |
| Next free CTID in the 300+ block | lowest unused id ≥ 300 across CTs and VMs |
| Free static IP (`.125`/`.126` reused/uncertain, `.130` = switch) | ping + ARP + configured-IP scan of .131–.160, never proposes .125/.126/.130. **Confirm in UniFi** that no lease/reservation uses it. |
| Immich URL + version on CT101, unchanged | `curl http://<CT101 IP>:2283/api/server/version` (public endpoint, no key) |
| CT can reach Immich :2283 and outbound HTTPS | after creation: `scripts/verify.sh` inside the CT |
| SSD-backed storage for the index | `lsblk ROTA` + `pvesm status`; main pool is SMR |
| How the tunnel publishes `boltbins.thestrzs.net` | prints only `hostname:`/`service:` lines from any CT's cloudflared config; changes nothing |

## Stop-gate proposal (to fill from the output)
| Item | Proposal |
|---|---|
| CTID | _next free ≥ 300_ |
| IP | _first free address in the probe_ |
| Hostname | `familysearch` |
| Public hostname | `search.thestrzs.net` (David's one approved subdomain exception) |
| Root disk | 80 GB on _storage_ |
| Meilisearch data | `--mp0 <ssd-storage>:40,mp=/var/lib/meilisearch` if an SSD-backed storage exists |
| Size | unprivileged Debian 12, 4 vCPU, 8 GB RAM, `nesting=1` |
| DNS | 192.168.0.40 (AdGuard) |

After approval: `bash scripts/create-ct.sh --ctid … --ip … --storage … [--ssd …]`
(dry run by default; add `--yes`).

## Things only the live system can answer
- Which LAN IP runs `cloudflared` (→ `CLOUDFLARED_IP` in `site.env`).
- Immich version, and the exact API-key permissions it needs. Expected for
  current Immich: `album.read`, `asset.read` (only for the album-scoped
  metadata-search fallback) and `asset.view` (thumbnail/preview). Confirm with
  `python -m familysearch.cli check-immich` and grant nothing more.
- Whether Immich smart search includes albums shared with `family-search`.
  Version 1 does not use smart search at all: photo search is the local
  album-name / date / caption index, so the question only matters for a v2.
- Whether the pinned `MEILI_VERSION` tag (v1.15.2) is still the one you want;
  the installer refuses to install without a verified sha256.
