#!/bin/bash
# =============================================================================
#  jellyclean-nas - NFS pohrana za JellyClean (instalira se u /usr/local/sbin,
#  pokreće se kao root: sudo -n jellyclean-nas … iz servisa JellyClean)
#
#  Smije SAMO upravljati NFS mountovima ispod /mnt/jellyclean-nas/<id>,
#  zapisanima u /etc/fstab s oznakom "# jellyclean-nas:<id>".
#    status                     popis mountova JellyCleana i jesu li montirani
#    exports <host>             NFS exporti NAS-a (showmount -e)
#    test <host:/export> <ver>  probno montiranje (samo čitanje) i odmah odmontiranje
#    add <id> <host:/export> <ver>   upiše u fstab i montira
#    mount <id> | umount <id>
#    remove <id>                odmontira i ukloni iz fstaba
#  <ver>: auto | 3 | 4 | 4.1 | 4.2
# =============================================================================
set -uo pipefail
export LC_ALL=C PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
BASE=/mnt/jellyclean-nas
FSTAB=/etc/fstab
TAG="# jellyclean-nas:"

err() { echo "ERR $*"; exit 1; }
okid()  { [[ ${1:-} =~ ^[a-z0-9][a-z0-9-]{0,23}$ ]] || err "neispravan ID pohrane"; }
oksrc() { [[ ${1:-} =~ ^[A-Za-z0-9][A-Za-z0-9.-]*:/[A-Za-z0-9._/@+-]*$ ]] || err "neispravan NFS izvor (oblik NAS:/putanja, bez razmaka)"; }
okhost(){ [[ ${1:-} =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$ ]] || err "neispravan NAS"; }
opts() {
  local o="rw,hard,noatime,_netdev,nofail,x-systemd.mount-timeout=30"
  case ${1:-auto} in auto) ;; 3|4|4.1|4.2) o+=",vers=$1" ;; *) err "neispravna verzija NFS-a" ;; esac
  printf '%s' "$o"
}
need_nfs() { command -v mount.nfs >/dev/null 2>&1 || err "u kontejneru nema nfs-common (apt install nfs-common)"; }
entry() { awk -v t="$TAG$1" 'f{print; exit} $0==t{f=1}' "$FSTAB"; }

op=${1:-status}; shift || true
case $op in
  status)
    while IFS= read -r line; do
      id=${line#"$TAG"}; e=$(entry "$id"); [[ -n $e ]] || continue
      read -r src mp _ o _ <<<"$e"
      m=0; mountpoint -q "$mp" 2>/dev/null && m=1
      df=$( ((m)) && df -B1 -P "$mp" 2>/dev/null | awk 'NR==2{print $2, $3, $4}')
      echo "NAS $id $m $src $mp ${df:-- - -} $o"
    done < <(grep "^$TAG" "$FSTAB" 2>/dev/null)
    echo "END" ;;
  exports)
    okhost "${1:-}"; need_nfs
    command -v showmount >/dev/null 2>&1 || err "showmount nije dostupan"
    out=$(timeout 15 showmount -e --no-headers "$1" 2>&1) || err "NAS $1 nije vratio popis exporta (NFSv4-only NAS? upiši export ručno): ${out:0:200}"
    awk '{print "EXPORT", $1, $2}' <<<"$out"
    echo "END" ;;
  test)
    oksrc "${1:-}"; need_nfs; o=$(opts "${2:-auto}")
    t=$(mktemp -d /run/jellyclean-nas-test.XXXXXX)
    if out=$(timeout 40 mount -t nfs -o "ro,${o#rw,}" "$1" "$t" 2>&1); then
      n=$(find "$t" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l)
      df=$(df -B1 -P "$t" | awk 'NR==2{print $2, $3, $4}')
      umount "$t" 2>/dev/null || umount -l "$t"; rmdir "$t"
      echo "OK $n $df"
    else
      rmdir "$t" 2>/dev/null; err "montiranje nije uspjelo: ${out:0:300}"
    fi ;;
  add)
    okid "${1:-}"; oksrc "${2:-}"; need_nfs; o=$(opts "${3:-auto}")
    grep -qx "$TAG$1" "$FSTAB" && err "pohrana $1 već postoji"
    mp="$BASE/$1"; mkdir -p "$mp"
    printf '%s\n%s %s nfs %s 0 0\n' "$TAG$1" "$2" "$mp" "$o" >> "$FSTAB"
    systemctl daemon-reload >/dev/null 2>&1 || true
    if out=$(timeout 40 mount "$mp" 2>&1); then echo "MOUNTED $mp"
    else
      sed -i "\|^$TAG$1\$|,+1d" "$FSTAB"; rmdir "$mp" 2>/dev/null
      err "montiranje nije uspjelo: ${out:0:300}"
    fi ;;
  mount)
    okid "${1:-}"; mp="$BASE/$1"; [[ -n $(entry "$1") ]] || err "pohrana $1 ne postoji"
    mountpoint -q "$mp" && { echo "MOUNTED $mp"; exit 0; }
    out=$(timeout 40 mount "$mp" 2>&1) && echo "MOUNTED $mp" || err "montiranje nije uspjelo: ${out:0:300}" ;;
  umount)
    okid "${1:-}"; mp="$BASE/$1"
    mountpoint -q "$mp" && { umount "$mp" 2>/dev/null || umount -l "$mp"; }
    echo "UNMOUNTED" ;;
  remove)
    okid "${1:-}"; mp="$BASE/$1"
    mountpoint -q "$mp" && { umount "$mp" 2>/dev/null || umount -l "$mp"; }
    sed -i "\|^$TAG$1\$|,+1d" "$FSTAB"
    systemctl daemon-reload >/dev/null 2>&1 || true
    rmdir "$mp" 2>/dev/null || true
    echo "REMOVED" ;;
  version) echo "NASVER 1" ;;
  *) err "nepoznata radnja" ;;
esac
