#!/bin/bash
# =============================================================================
#  JellyClean - koraci nakon instalacije/nadogradnje (UNUTAR kontejnera, kao root)
#    • nfs-common (NFS pohrana)
#    • /usr/local/sbin/jellyclean-nas + sudoers pravilo za korisnika servisa
#  Pokreće ga jellyclean-update automatski; ručno:  bash /opt/jellyclean/proxmox/lxc-post-update.sh
# =============================================================================
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Pokreni kao root"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)/jellyclean-nas.sh"
[ -f "$SRC" ] || { echo "Nema $SRC"; exit 1; }

if ! command -v mount.nfs >/dev/null 2>&1; then
  echo "➜ instaliram nfs-common…"
  DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nfs-common >/dev/null \
    || echo "  ⚠ nfs-common se ne može instalirati - NFS pohrana neće raditi"
fi

install -o root -g root -m 755 "$SRC" /usr/local/sbin/jellyclean-nas.new
mv -f /usr/local/sbin/jellyclean-nas.new /usr/local/sbin/jellyclean-nas
mkdir -p /mnt/jellyclean-nas

U=$(systemctl show -p User --value jellyclean 2>/dev/null || true)
if [ -n "$U" ] && [ "$U" != root ]; then
  command -v sudo >/dev/null 2>&1 || { DEBIAN_FRONTEND=noninteractive apt-get install -y -qq sudo >/dev/null || true; }
  if command -v sudo >/dev/null 2>&1; then
    printf '%s\n' "# JellyClean: $U smije bez lozinke pokretati SAMO jellyclean-nas (NFS pohrana)" \
                  "$U ALL=(root) NOPASSWD: /usr/local/sbin/jellyclean-nas" > /etc/sudoers.d/jellyclean-nas.new
    chmod 440 /etc/sudoers.d/jellyclean-nas.new
    if visudo -cf /etc/sudoers.d/jellyclean-nas.new >/dev/null; then mv -f /etc/sudoers.d/jellyclean-nas.new /etc/sudoers.d/jellyclean-nas
    else rm -f /etc/sudoers.d/jellyclean-nas.new; echo "  ⚠ sudoers pravilo nije ispravno - preskačem"; fi
  fi
fi
# lxc-a bez privilegija/značajke mount=nfs: NFS se ne može montirati u kontejneru
if grep -qa 'container=lxc' /proc/1/environ 2>/dev/null && [ "$(awk '/^[ \t]*0[ \t]/{print $2; exit}' /proc/self/uid_map 2>/dev/null)" != 0 ]; then
  echo "  ⚠ kontejner je neprivilegiran - NFS pohranu montiraj na Proxmox hostu i proslijedi je kao mount point"
fi
echo "✔ jellyclean-nas v$(sed -n 's/.*echo "NASVER \([0-9]*\)".*/\1/p' /usr/local/sbin/jellyclean-nas)"
