#!/usr/bin/env bash
# Create the familysearch CT on the Proxmox HOST — only after David has approved
# the CTID / IP / storage from PREFLIGHT.md. Prints the exact command and does
# nothing unless --yes is given.
#   bash create-ct.sh --ctid 3xx --ip 192.168.0.x --storage local-zfs [--ssd ssd-pool] [--gw 192.168.0.1] [--bridge vmbr0] [--yes]
set -euo pipefail
CTID='' IP='' STORAGE='' SSD='' GW=192.168.0.1 BRIDGE=vmbr0 YES=0
while [ $# -gt 0 ]; do case $1 in
  --ctid) CTID=$2; shift 2;; --ip) IP=$2; shift 2;; --storage) STORAGE=$2; shift 2;;
  --ssd) SSD=$2; shift 2;; --gw) GW=$2; shift 2;; --bridge) BRIDGE=$2; shift 2;; --yes) YES=1; shift;;
  *) echo "unknown option $1" >&2; exit 2;; esac; done
[ -n "$CTID" ] && [ -n "$IP" ] && [ -n "$STORAGE" ] || { sed -n '2,6p' "$0"; exit 2; }
[ "$CTID" -ge 300 ] || { echo "CTID must be in the 300+ block" >&2; exit 1; }
for bad in 100 101 102 104 199 300 304; do [ "$CTID" = "$bad" ] && { echo "CT$CTID is off-limits" >&2; exit 1; }; done
pct status "$CTID" >/dev/null 2>&1 && { echo "CT$CTID already exists — refusing" >&2; exit 1; }
case $IP in 192.168.0.125|192.168.0.126|192.168.0.130) echo "$IP is reserved/uncertain per the notes" >&2; exit 1;; esac
ping -c1 -W1 "$IP" >/dev/null 2>&1 && { echo "$IP answers ping — not free" >&2; exit 1; }
TEMPLATE=$(pveam list local 2>/dev/null | awk '/debian-12-standard/ {print $1}' | sort -V | tail -1)
[ -n "$TEMPLATE" ] || { echo "No Debian 12 template. Run: pveam update && pveam download local \$(pveam available | awk '/debian-12-standard/ {print \$2}' | tail -1)" >&2; exit 1; }
CMD=(pct create "$CTID" "$TEMPLATE" --hostname familysearch --unprivileged 1 --features nesting=1
     --cores 4 --memory 8192 --swap 1024 --rootfs "$STORAGE:80"
     --net0 "name=eth0,bridge=$BRIDGE,ip=$IP/24,gw=$GW,firewall=0" --nameserver 192.168.0.40
     --onboot 1 --start 0 --description "Family Search (search.thestrzs.net)")
[ -n "$SSD" ] && CMD+=(--mp0 "$SSD:40,mp=/var/lib/meilisearch,backup=1")
echo "Will run:"; printf '  %q' "${CMD[@]}"; echo
[ "$YES" = 1 ] || { echo "(dry run — add --yes to create it)"; exit 0; }
"${CMD[@]}"
pct start "$CTID"
echo "CT$CTID created and started. Next: copy the source in and run scripts/install.sh inside it (see README)."
