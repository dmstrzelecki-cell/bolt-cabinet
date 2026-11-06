#!/usr/bin/env bash
# READ-ONLY preflight. Run as root on the Proxmox HOST (T320). It changes
# nothing: no pct create/set, no zfs/zpool changes, no writes to any CT.
# Output is Markdown on stdout; save it as PREFLIGHT.md wherever you like:
#   bash preflight.sh > /tmp/PREFLIGHT.md
# The only files read inside other CTs are cloudflared ingress hostnames.
set -uo pipefail
cand_from=${CAND_FROM:-131}; cand_to=${CAND_TO:-160}
h() { printf '\n## %s\n\n' "$*"; }
code() { printf '```\n'; "$@" 2>&1; printf '```\n'; }

echo "# Family Search — preflight ($(hostname), $(date -Is))"
h "Host"
code pveversion
echo; echo "CPU threads: $(nproc)"
echo; echo "**RAM (actual):** $(free -g | awk '/^Mem:/ {print $2 " GiB total, " $7 " GiB available"}')"
code free -h

h "Containers and VMs"
code pct list
code qm list
echo
echo "| CTID | hostname | IP (net0) | memory | cores | rootfs |"
echo "|---|---|---|---|---|---|"
for id in $(pct list | awk 'NR>1 {print $1}'); do
  c=$(pct config "$id" 2>/dev/null)
  ip=$(sed -n 's/^net0:.*ip=\([^,]*\).*/\1/p' <<<"$c")
  printf '| %s | %s | %s | %s | %s | %s |\n' "$id" "$(sed -n 's/^hostname: //p' <<<"$c")" "${ip:-?}" \
    "$(sed -n 's/^memory: //p' <<<"$c")" "$(sed -n 's/^cores: //p' <<<"$c")" "$(sed -n 's/^rootfs: //p' <<<"$c" | cut -d, -f1-2)"
done

h "Next free CTID in the 300+ block"
used=$( { pct list | awk 'NR>1 {print $1}'; qm list | awk 'NR>1 {print $1}'; } | sort -n)
next=300; while grep -qx "$next" <<<"$used"; do next=$((next + 1)); done
echo "**Proposed CTID: $next**"

h "Storage"
code pvesm status
command -v zpool >/dev/null && code zpool list
echo; echo "Disks (ROTA=0 means SSD/NVMe):"
code lsblk -d -o NAME,ROTA,SIZE,MODEL,TRAN
echo; echo "Storage backed by non-rotational disks (candidates for the Meilisearch mount point):"
code bash -c 'for s in $(pvesm status | awk "NR>1 {print \$1}"); do printf "%s: " "$s"; pvesm path "$s:x" 2>/dev/null | head -1 || echo; done'

h "Free static IP"
echo "Addresses already configured on CTs/VMs:"
taken=$( { for id in $(pct list | awk 'NR>1{print $1}'); do pct config "$id" | sed -n 's/^net[0-9]*:.*ip=\([0-9.]*\).*/\1/p'; done; ip neigh | awk '{print $1}'; } | sort -u)
code echo "$taken"
echo; echo "Probe of 192.168.0.${cand_from}-${cand_to} (ping + ARP). Notes flag .125/.126 as reused/uncertain and .130 as the switch, so they are never proposed:"
echo; echo "| IP | ping | ARP | configured | verdict |"; echo "|---|---|---|---|---|"
proposed=""
for n in $(seq "$cand_from" "$cand_to"); do
  ipa=192.168.0.$n
  case $n in 125|126|130) continue;; esac
  p=no; ping -c1 -W1 "$ipa" >/dev/null 2>&1 && p=yes
  a=$(ip neigh show "$ipa" | grep -q lladdr && echo yes || echo no)
  cfg=$(grep -qx "$ipa" <<<"$taken" && echo yes || echo no)
  v=free; { [ $p = yes ] || [ $a = yes ] || [ $cfg = yes ]; } && v="IN USE"
  printf '| %s | %s | %s | %s | %s |\n' "$ipa" "$p" "$a" "$cfg" "$v"
  [ -z "$proposed" ] && [ "$v" = free ] && proposed=$ipa
done
echo; echo "**Proposed IP: ${proposed:-none found — widen CAND_FROM/CAND_TO}** (confirm in UniFi that no DHCP lease/reservation uses it)"

h "Immich on CT101 (read-only)"
ip101=$(pct config 101 2>/dev/null | sed -n 's/^net0:.*ip=\([0-9.]*\).*/\1/p')
echo "CT101 IP: ${ip101:-unknown}"
[ -n "$ip101" ] && code curl -s -m 5 "http://$ip101:2283/api/server/version"
echo; echo "(No API key used; /api/server/version is public.)"

h "Cloudflare Tunnel (how boltbins.thestrzs.net is published)"
for id in $(pct list | awk 'NR>1 && $2=="running" {print $1}'); do
  out=$(pct exec "$id" -- sh -c 'command -v cloudflared >/dev/null 2>&1 || exit 3; echo "cloudflared: $(cloudflared --version 2>/dev/null | head -1)"; systemctl is-active cloudflared 2>/dev/null; for f in /etc/cloudflared/config.yml /root/.cloudflared/config.yml; do [ -f "$f" ] && grep -E "^\s*(-\s*)?(hostname|service):" "$f"; done' 2>/dev/null)
  [ $? -eq 3 ] && continue
  [ -n "$out" ] && { echo "### CT $id"; printf '```\n%s\n```\n' "$out"; }
done
command -v cloudflared >/dev/null && echo "cloudflared is also installed on the host itself."
echo
echo "If no ingress lines appear, the tunnel is remotely managed (token) — its routes live in the Cloudflare dashboard. Nothing was changed."

h "Stop gate — proposal for David"
cat <<EOP
| Item | Proposal |
|---|---|
| CTID | $next |
| IP | ${proposed:-?}/24 |
| Hostname | familysearch |
| Public hostname | search.thestrzs.net (approved subdomain exception) |
| Root disk | 80 GB on <pick from Storage above> |
| Meilisearch data | separate mount point on an SSD-backed storage if one is listed above |
| Size | unprivileged Debian 12, 4 vCPU, 8 GB RAM, nesting=1 |
| DNS | 192.168.0.40 (AdGuard) |

Nothing is created until David approves. Then: \`bash create-ct.sh --ctid $next --ip ${proposed:-IP} --storage <rootfs-storage> [--ssd <ssd-storage>]\`
EOP
