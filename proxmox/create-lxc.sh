#!/usr/bin/env bash
# ============================================================================
#  JellyClean - kreiranje Proxmox LXC kontejnera
#  Pokreni na Proxmox HOSTU kao root:
#     bash -c "$(curl -fsSL https://raw.githubusercontent.com/moordrak/JellyClean/main/proxmox/create-lxc.sh)"
#  ili iz kloniranog repozitorija:
#     bash proxmox/create-lxc.sh
# ============================================================================
set -Eeuo pipefail

REPO_URL_DEFAULT="https://github.com/moordrak/JellyClean.git"
APP_DIR="/opt/jellyclean"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /tmp)"
LOCAL_SRC="$(cd "$SCRIPT_DIR/.." 2>/dev/null && pwd || echo /nonexistent)"
TITLE="JellyClean LXC"
NONINTERACTIVE="${NONINTERACTIVE:-0}"

# ---------------------------------------------------------------- izgled ----
if [[ -t 1 ]]; then
  C_OK=$'\e[32m'; C_ERR=$'\e[31m'; C_INF=$'\e[36m'; C_WRN=$'\e[33m'; C_B=$'\e[1m'; C_0=$'\e[0m'
else
  C_OK=""; C_ERR=""; C_INF=""; C_WRN=""; C_B=""; C_0=""
fi
info() { echo "${C_INF}➜${C_0} $*"; }
ok()   { echo "${C_OK}✔${C_0} $*"; }
warn() { echo "${C_WRN}!${C_0} $*"; }
die()  { echo "${C_ERR}✘ $*${C_0}" >&2; exit 1; }
trap 'die "Greška u liniji $LINENO: $BASH_COMMAND"' ERR

USE_WT=0
if [[ "$NONINTERACTIVE" != "1" ]] && command -v whiptail >/dev/null && [[ -t 0 ]]; then USE_WT=1; fi

# ask VAR "pitanje" "default"
ask() {
  local __var=$1 q=$2 def=${3:-} ans
  if [[ "$NONINTERACTIVE" == "1" ]]; then printf -v "$__var" '%s' "$def"; return; fi
  if (( USE_WT )); then
    ans=$(whiptail --title "$TITLE" --inputbox "$q" 12 78 "$def" 3>&1 1>&2 2>&3) || die "Prekinuto."
  else
    printf '%b [%s]: ' "$q" "$def"; read -r ans || true; ans=${ans:-$def}
  fi
  printf -v "$__var" '%s' "$ans"
}
# ask_secret VAR "pitanje" "default"
ask_secret() {
  local __var=$1 q=$2 def=${3:-} ans
  if [[ "$NONINTERACTIVE" == "1" ]]; then printf -v "$__var" '%s' "$def"; return; fi
  if (( USE_WT )); then
    ans=$(whiptail --title "$TITLE" --passwordbox "$q\n\n(prazno = ${def:+zadana vrijednost}${def:-ništa})" 12 78 3>&1 1>&2 2>&3) || die "Prekinuto."
  else
    printf '%b: ' "$q"; read -r -s ans || true; echo
  fi
  printf -v "$__var" '%s' "${ans:-$def}"
}
# yesno "pitanje" default(y|n) -> 0/1
yesno() {
  local q=$1 def=${2:-y} ans
  if [[ "$NONINTERACTIVE" == "1" ]]; then [[ "$def" == y ]]; return; fi
  if (( USE_WT )); then
    if [[ "$def" == y ]]; then whiptail --title "$TITLE" --yesno "$q" 12 78; else whiptail --title "$TITLE" --defaultno --yesno "$q" 12 78; fi
    return
  fi
  printf '%b [%s]: ' "$q" "$( [[ $def == y ]] && echo D/n || echo d/N )"; read -r ans || true; ans=${ans:-$def}
  [[ "$ans" =~ ^[yYdD] ]]
}
# menu VAR "pitanje" default tag1 opis1 tag2 opis2 ...
menu() {
  local __var=$1 q=$2 def=$3; shift 3
  local ans
  if [[ "$NONINTERACTIVE" == "1" ]]; then printf -v "$__var" '%s' "$def"; return; fi
  if (( USE_WT )); then
    local n=$(( $# / 2 )); (( n > 12 )) && n=12
    ans=$(whiptail --title "$TITLE" --default-item "$def" --menu "$q" $((n + 10)) 78 "$n" "$@" 3>&1 1>&2 2>&3) || die "Prekinuto."
  else
    printf '%b\n' "$q"; local a=("$@") i
    for ((i = 0; i < ${#a[@]}; i += 2)); do echo "   ${a[i]}) ${a[i+1]}"; done
    printf 'Odabir [%s]: ' "$def"; read -r ans || true; ans=${ans:-$def}
  fi
  printf -v "$__var" '%s' "$ans"
}
msg() { if (( USE_WT )); then whiptail --title "$TITLE" --msgbox "$1" 16 78; else echo -e "\n$1\n"; fi; }

# vrijednost za systemd EnvironmentFile
envq() { local v=${1//\\/\\\\}; v=${v//\"/\\\"}; printf '"%s"' "$v"; }

# ------------------------------------------------------------ provjere -----
[[ $EUID -eq 0 ]] || die "Pokreni kao root na Proxmox hostu."
command -v pct >/dev/null && command -v pveam >/dev/null || die "Ovo nije Proxmox VE host (nema pct/pveam)."

msg "Ova skripta kreira LXC kontejner s JellyClean servisom.\n\nPostavit će pitanja o:\n • kontejneru (ID, resursi, mreža)\n • medijskim mapama (bind mount s hosta ili NFS)\n • Jellyfin API-ju\n • brisanju, kanti i lozinki\n\nNa kraju prikazuje sažetak prije kreiranja."

# ============================================================ 1. KONTEJNER ==
NEXTID=$(pvesh get /cluster/nextid 2>/dev/null || echo 120)
while :; do
  ask CTID "ID kontejnera" "$NEXTID"
  [[ "$CTID" =~ ^[0-9]+$ ]] && (( CTID >= 100 )) || { msg "ID mora biti broj >= 100."; continue; }
  if pct status "$CTID" &>/dev/null || qm status "$CTID" &>/dev/null; then msg "ID $CTID je već zauzet."; continue; fi
  break
done
ask CT_HOST "Hostname" "jellyclean"

# --- storage za predloške
mapfile -t TSTORES < <(pvesm status -content vztmpl 2>/dev/null | awk 'NR>1 && $3=="active"{print $1}')
(( ${#TSTORES[@]} )) || die "Nema aktivnog storagea za LXC predloške (content: vztmpl)."
if (( ${#TSTORES[@]} == 1 )); then TSTORE=${TSTORES[0]}; else
  args=(); for s in "${TSTORES[@]}"; do args+=("$s" "predlošci"); done
  menu TSTORE "Storage za LXC predložak (template)" "${TSTORES[0]}" "${args[@]}"
fi

# --- OS predložak
menu OS_CHOICE "Operativni sustav kontejnera" "debian-12" \
  "debian-12" "Debian 12 (preporučeno)" \
  "debian-13" "Debian 13" \
  "ubuntu-24.04" "Ubuntu 24.04 LTS"

# --- storage za disk
mapfile -t RSTORES < <(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active"{printf "%s %d\n",$1,$6/1048576}')
(( ${#RSTORES[@]} )) || die "Nema aktivnog storagea za LXC diskove (content: rootdir)."
args=(); for s in "${RSTORES[@]}"; do args+=("${s%% *}" "slobodno ${s##* } GB"); done
menu RSTORE "Storage za disk kontejnera" "${RSTORES[0]%% *}" "${args[@]}"

ask DISK "Veličina diska (GB)" "4"
ask CORES "Broj CPU jezgri" "2"
ask RAM "RAM (MB)" "1024"
ask SWAP "Swap (MB)" "512"
[[ "$DISK$CORES$RAM$SWAP" =~ ^[0-9]+$ ]] || die "Disk, jezgre, RAM i swap moraju biti brojevi."

# ================================================================ 2. MREŽA ==
mapfile -t BRIDGES < <(ip -o link show type bridge 2>/dev/null | awk -F': ' '{print $2}' | grep -E '^vmbr' || true)
(( ${#BRIDGES[@]} )) || BRIDGES=(vmbr0)
if (( ${#BRIDGES[@]} == 1 )); then BRIDGE=${BRIDGES[0]}; else
  args=(); for b in "${BRIDGES[@]}"; do args+=("$b" "bridge"); done
  menu BRIDGE "Mrežni bridge" "${BRIDGES[0]}" "${args[@]}"
fi
ask VLAN "VLAN tag (prazno = bez VLAN-a)" ""
[[ -z "$VLAN" || "$VLAN" =~ ^[0-9]+$ ]] || die "VLAN mora biti broj."

menu NETMODE "IP adresa kontejnera" "dhcp" "dhcp" "DHCP (UniFi dodjeljuje adresu)" "static" "Statička IP adresa"
if [[ "$NETMODE" == static ]]; then
  while :; do
    ask IPCIDR "IP adresa s maskom (npr. 192.168.1.50/24)" ""
    [[ "$IPCIDR" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$ ]] && break; msg "Neispravan format. Primjer: 192.168.1.50/24"
  done
  ask GW "Gateway" "$(echo "$IPCIDR" | cut -d/ -f1 | awk -F. '{print $1"."$2"."$3".1"}')"
  ask DNS "DNS poslužitelj (prazno = kao host)" ""
  NET0="name=eth0,bridge=$BRIDGE,ip=$IPCIDR,gw=$GW"
else
  DNS=""
  NET0="name=eth0,bridge=$BRIDGE,ip=dhcp"
fi
[[ -n "$VLAN" ]] && NET0+=",tag=$VLAN"

# ===================================================== 3. TIP KONTEJNERA ==
menu CT_TYPE "Vrsta kontejnera" "unpriv" \
  "unpriv" "Unprivileged (sigurnije, mediji preko bind mounta s hosta)" \
  "priv" "Privileged (potrebno ako se NFS montira UNUTAR kontejnera)"

# ======================================================= 4. MEDIJSKE MAPE ==
msg "Medijske mape\n\nJellyClean mora vidjeti iste mape s filmovima i serijama kao Jellyfin, i to s pravom PISANJA (inače ne može brisati).\n\nNačini:\n • bind mount: mapa koja je već montirana na Proxmox hostu (npr. NFS s NAS-a u /mnt/pve/nas) prosljeđuje se u kontejner\n • NFS: kontejner sam montira NFS share s NAS-a (samo privileged)"
if [[ "$CT_TYPE" == priv ]]; then
  menu MEDIA_MODE "Kako kontejner dobiva medije?" "bind" \
    "bind" "Bind mount mape s Proxmox hosta" \
    "nfs" "NFS mount unutar kontejnera"
else
  MEDIA_MODE=bind
fi

MEDIA_LABELS=(); MEDIA_SRC=(); MEDIA_DST=()
if [[ "$MEDIA_MODE" == nfs ]]; then ask NFS_SERVER "IP ili ime NAS-a (NFS server)" ""; fi
i=0
while :; do
  i=$((i + 1))
  defl=$([[ $i == 1 ]] && echo Filmovi || ([[ $i == 2 ]] && echo Serije || echo "Mapa$i"))
  ask L "Medijska mapa #$i - naziv (npr. Filmovi, Serije, Anime)" "$defl"
  if [[ "$MEDIA_MODE" == nfs ]]; then
    ask S "[$L] NFS export na NAS-u (npr. /volume1/media/movies)" ""
    [[ -n "$S" ]] || { warn "Prazan export, preskačem."; i=$((i - 1)); }
  else
    while :; do
      ask S "[$L] Putanja na Proxmox HOSTU (npr. /mnt/pve/nas/movies)" ""
      if [[ -z "$S" ]]; then break; fi
      [[ -d "$S" ]] && break
      yesno "Mapa '$S' ne postoji na hostu. Ipak je koristiti?" n && break
    done
    [[ -n "$S" ]] || { warn "Prazna putanja, preskačem."; i=$((i - 1)); }
  fi
  if [[ -n "${S:-}" ]]; then
    slug=$(echo "$L" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/^-//;s/-$//')
    ask D "[$L] Putanja UNUTAR kontejnera" "/mnt/media/${slug:-media$i}"
    MEDIA_LABELS+=("$L"); MEDIA_SRC+=("$S"); MEDIA_DST+=("$D")
  fi
  yesno "Dodati još jednu medijsku mapu?" "$([[ ${#MEDIA_LABELS[@]} -lt 2 ]] && echo y || echo n)" || break
done
(( ${#MEDIA_LABELS[@]} )) || die "Nije zadana nijedna medijska mapa."

# --- vlasništvo datoteka
ask RUN_UID "UID vlasnika medijskih datoteka (onaj s kojim rade Jellyfin/*arr, npr. 1000; 0 = root)" "1000"
ask RUN_GID "GID vlasnika medijskih datoteka" "$RUN_UID"
[[ "$RUN_UID$RUN_GID" =~ ^[0-9]+$ ]] || die "UID i GID moraju biti brojevi."
IDMAP=0
if [[ "$CT_TYPE" == unpriv && "$MEDIA_MODE" == bind && "$RUN_UID" != 0 ]]; then
  if yesno "Unprivileged kontejner pomiče UID-ove (UID $RUN_UID u kontejneru = $((100000 + RUN_UID)) na hostu), pa ne bi mogao brisati datoteke vlasnika $RUN_UID.\n\nMapirati UID/GID $RUN_UID:$RUN_GID 1:1 na host? (preporučeno)" y; then IDMAP=1; fi
fi

# --- Jellyfin mape (samo za pregled)
JF_LABELS=(); JF_SRC=(); JF_DST=()
if [[ "$MEDIA_MODE" == bind ]] && yesno "Prikazivati i Jellyfinove config/cache mape (samo čitanje)?\n\nTo ima smisla samo ako su te mape dostupne na Proxmox hostu. Ako Jellyfin radi u Dockeru unutar VM-a, odgovori NE i koristi Jellyfin zadatke održavanja u sučelju." n; then
  while :; do
    ask L "Naziv (npr. Config, Cache, Metadata)" "Config"
    ask S "[$L] Putanja na Proxmox hostu" ""
    [[ -n "$S" ]] && { JF_LABELS+=("$L"); JF_SRC+=("$S"); JF_DST+=("/mnt/jellyfin/$(echo "$L" | tr '[:upper:]' '[:lower:]')"); }
    yesno "Dodati još jednu Jellyfin mapu?" n || break
  done
fi

# ============================================================= 5. JELLYFIN ==
JF_URL=""; JF_KEY=""; JF_USERS=""; PATH_MAP=""; JF_TLS=true
if yesno "Povezati JellyClean s Jellyfin API-jem?\n\n(podaci o tome tko je što pogledao, brisanje odgledanog, zadaci održavanja)" y; then
  while :; do
    ask JF_URL "Jellyfin URL (npr. http://192.168.1.20:8096 ili https://jellyfin.home.int)" "http://"
    JF_URL=${JF_URL%/}
    [[ "$JF_URL" =~ ^https?://[^/]+ ]] && break; msg "URL mora počinjati s http:// ili https://"
  done
  ask_secret JF_KEY "Jellyfin API ključ (Nadzorna ploča → API ključevi → +)" ""
  if [[ -n "$JF_KEY" ]] && command -v curl >/dev/null; then
    code=$(curl -sk -o /dev/null -w '%{http_code}' -m 8 -H "X-Emby-Token: $JF_KEY" "$JF_URL/System/Info" 2>/dev/null || true); code=${code:-000}
    case $code in
      200) ok "Jellyfin API ključ radi." ;;
      401|403) warn "Jellyfin odbija ključ (HTTP $code)."; yesno "API ključ nije prihvaćen. Nastaviti svejedno?" n || die "Prekinuto." ;;
      *) warn "Jellyfin nije dostupan s hosta (HTTP $code)."; yesno "Jellyfin na $JF_URL nije dostupan s hosta. Nastaviti svejedno?" y || die "Prekinuto." ;;
    esac
  fi
  if [[ "$JF_URL" == https://* ]] && ! yesno "Provjeravati TLS certifikat Jellyfina?\n\n(odgovori NE ako koristiš samopotpisani certifikat za .int domenu)" y; then JF_TLS=false; fi
  ask JF_USERS "Koji se korisnici broje za 'pogledali svi'? (imena odvojena zarezom, prazno = svi)" ""
  # PATH_MAP
  pm_list=()
  for idx in "${!MEDIA_LABELS[@]}"; do
    ask JP "Pod kojom putanjom JELLYFIN vidi mapu '${MEDIA_LABELS[$idx]}'?\n(npr. /data/movies u Jellyfin Dockeru; ostavi isto ako je jednako)" "${MEDIA_DST[$idx]}"
    JP=${JP%/}
    [[ -n "$JP" && "$JP" != "${MEDIA_DST[$idx]}" ]] && pm_list+=("$JP=${MEDIA_DST[$idx]}")
  done
  PATH_MAP=$(IFS=';'; echo "${pm_list[*]:-}")
fi

# ====================================================== 6. PONAŠANJE ======
ALLOW_DELETE=true; TRASH=true; TRASH_DAYS=7
if yesno "Dopustiti brisanje iz sučelja?\n\n(NE = samo pregled)" y; then
  if yesno "Koristiti kantu?\n\nObrisano se prvo premješta u .jellyclean-trash na istom disku, može se vratiti, a trajno se briše nakon X dana." y; then
    ask TRASH_DAYS "Nakon koliko dana automatski isprazniti kantu? (0 = nikad)" "7"
  else TRASH=false; fi
else ALLOW_DELETE=false; fi
ask SCAN_H "Automatsko ponovno skeniranje svakih X sati (0 = isključeno)" "6"
ask PORT "Port web sučelja" "8095"
ask AUTH_USER "Korisničko ime za prijavu u sučelje (prazno = bez lozinke)" "admin"
GEN_PASS=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16 || true)
AUTH_PASS=""
[[ -n "$AUTH_USER" ]] && ask_secret AUTH_PASS "Lozinka za '$AUTH_USER' (prazno = generiraj)" "$GEN_PASS"

# =========================================================== 7. IZVOR KODA ==
SRC_OPTS=("git" "Kloniraj s GitHuba (javni repozitorij)" "token" "Kloniraj s GitHuba uz token (privatni repozitorij)")
SRC_DEF=git
if [[ -f "$LOCAL_SRC/app.py" && -d "$LOCAL_SRC/static" ]]; then SRC_OPTS+=("local" "Kopiraj iz $LOCAL_SRC"); SRC_DEF=local; fi
menu SRC "Odakle instalirati JellyClean?" "$SRC_DEF" "${SRC_OPTS[@]}"
REPO_URL=$REPO_URL_DEFAULT; GH_TOKEN=""
if [[ "$SRC" != local ]]; then ask REPO_URL "Git repozitorij" "$REPO_URL_DEFAULT"; fi
if [[ "$SRC" == token ]]; then ask_secret GH_TOKEN "GitHub token (fine-grained, samo Contents: read)" ""; [[ -n "$GH_TOKEN" ]] || die "Token je obavezan."; fi
ONBOOT=1; yesno "Pokretati kontejner automatski pri pokretanju Proxmoxa?" y || ONBOOT=0

# ============================================================== SAŽETAK ====
summary="Kontejner:  $CTID ($CT_HOST), $([[ $CT_TYPE == priv ]] && echo privileged || echo unprivileged), $OS_CHOICE
Resursi:    ${CORES} CPU, ${RAM} MB RAM, ${DISK} GB na $RSTORE
Mreža:      $BRIDGE${VLAN:+ VLAN $VLAN}, $([[ $NETMODE == static ]] && echo "$IPCIDR gw $GW" || echo DHCP)
Mediji ($MEDIA_MODE), vlasnik $RUN_UID:$RUN_GID$([[ $IDMAP == 1 ]] && echo ", 1:1 idmap"):"
for idx in "${!MEDIA_LABELS[@]}"; do summary+=$'\n'"  ${MEDIA_LABELS[$idx]}: ${MEDIA_SRC[$idx]} → ${MEDIA_DST[$idx]}"; done
for idx in "${!JF_LABELS[@]}"; do summary+=$'\n'"  [ro] ${JF_LABELS[$idx]}: ${JF_SRC[$idx]} → ${JF_DST[$idx]}"; done
summary+="
Jellyfin:   ${JF_URL:-nije povezan}${PATH_MAP:+
PATH_MAP:   $PATH_MAP}
Brisanje:   $ALLOW_DELETE, kanta: $TRASH ($TRASH_DAYS d), skeniranje svakih ${SCAN_H}h
Sučelje:    port $PORT, prijava: ${AUTH_USER:-BEZ LOZINKE}
Izvor:      $SRC"
if (( USE_WT )); then
  whiptail --title "$TITLE - sažetak" --yesno "$summary\n\nKreirati kontejner?" 30 90 || die "Prekinuto."
else
  echo; echo "$summary"; echo; yesno "Kreirati kontejner?" y || die "Prekinuto."
fi

# ============================================================== KREIRANJE ==
info "Tražim predložak $OS_CHOICE…"
pveam update >/dev/null 2>&1 || warn "pveam update nije uspio, koristim lokalni popis."
case $OS_CHOICE in
  debian-12) PAT='^debian-12-standard_.*amd64' ;;
  debian-13) PAT='^debian-13-standard_.*amd64' ;;
  ubuntu-24.04) PAT='^ubuntu-24\.04-standard_.*amd64' ;;
esac
TEMPLATE=$(pveam list "$TSTORE" 2>/dev/null | awk 'NR>1{print $1}' | sed 's#.*/##' | grep -E "$PAT" | sort -V | tail -1 || true)
if [[ -z "$TEMPLATE" ]]; then
  TEMPLATE=$(pveam available --section system 2>/dev/null | awk '{print $2}' | grep -E "$PAT" | sort -V | tail -1 || true)
  [[ -n "$TEMPLATE" ]] || die "Predložak za $OS_CHOICE nije pronađen (pveam available)."
  info "Preuzimam $TEMPLATE…"
  pveam download "$TSTORE" "$TEMPLATE" >/dev/null
fi
ok "Predložak: $TEMPLATE"

FEATURES="nesting=1"
[[ "$MEDIA_MODE" == nfs ]] && FEATURES+=",mount=nfs"
create_args=(
  --hostname "$CT_HOST" --cores "$CORES" --memory "$RAM" --swap "$SWAP"
  --rootfs "$RSTORE:$DISK" --net0 "$NET0" --ostype "${OS_CHOICE%%-*}"
  --unprivileged "$([[ $CT_TYPE == priv ]] && echo 0 || echo 1)"
  --features "$FEATURES" --onboot "$ONBOOT" --start 0
  --description "JellyClean - pregled i čišćenje Jellyfin prostora
http://<ip>:$PORT"
  --tags "jellyclean;media"
)
[[ -n "$DNS" ]] && create_args+=(--nameserver "$DNS")
info "Kreiram kontejner $CTID…"
pct create "$CTID" "$TSTORE:vztmpl/$TEMPLATE" "${create_args[@]}" >/dev/null
ok "Kontejner $CTID kreiran."

# --- bind mountovi
mp=0
if [[ "$MEDIA_MODE" == bind ]]; then
  for idx in "${!MEDIA_LABELS[@]}"; do
    pct set "$CTID" "-mp$mp" "${MEDIA_SRC[$idx]},mp=${MEDIA_DST[$idx]},backup=0"; mp=$((mp + 1))
  done
fi
for idx in "${!JF_LABELS[@]}"; do
  pct set "$CTID" "-mp$mp" "${JF_SRC[$idx]},mp=${JF_DST[$idx]},ro=1,backup=0"; mp=$((mp + 1))
done
(( mp )) && ok "Dodano $mp bind mountova."

# --- 1:1 mapiranje UID/GID
if (( IDMAP )); then
  info "Postavljam 1:1 mapiranje UID $RUN_UID / GID $RUN_GID…"
  u=$RUN_UID; g=$RUN_GID
  cat >> "/etc/pve/lxc/$CTID.conf" <<EOF
lxc.idmap: u 0 100000 $u
lxc.idmap: u $u $u 1
lxc.idmap: u $((u + 1)) $((100000 + u + 1)) $((65536 - u - 1))
lxc.idmap: g 0 100000 $g
lxc.idmap: g $g $g 1
lxc.idmap: g $((g + 1)) $((100000 + g + 1)) $((65536 - g - 1))
EOF
  grep -qx "root:$u:1" /etc/subuid || echo "root:$u:1" >> /etc/subuid
  grep -qx "root:$g:1" /etc/subgid || echo "root:$g:1" >> /etc/subgid
  ok "idmap postavljen."
fi

info "Pokrećem kontejner…"
pct start "$CTID"
for _ in $(seq 1 30); do
  pct exec "$CTID" -- bash -c 'ping -c1 -W1 deb.debian.org >/dev/null 2>&1 || ping -c1 -W1 1.1.1.1 >/dev/null 2>&1' && break
  sleep 2
done
pct exec "$CTID" -- bash -c 'ping -c1 -W2 1.1.1.1 >/dev/null 2>&1' || die "Kontejner nema mrežu. Provjeri bridge/VLAN/IP pa ga obriši s 'pct destroy $CTID'."
ok "Mreža radi."

# ---------------------------------------------------------- instalacija ----
info "Instaliram pakete (python3, git, curl$([[ $MEDIA_MODE == nfs ]] && echo ", nfs-common"))…"
PKGS="python3 git curl ca-certificates"
[[ "$MEDIA_MODE" == nfs ]] && PKGS+=" nfs-common"
pct exec "$CTID" -- bash -c "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get -y -qq upgrade >/dev/null && apt-get install -y -qq $PKGS >/dev/null"
ok "Paketi instalirani."

# --- korisnik
if [[ "$RUN_UID" == 0 ]]; then RUN_USER=root; else
  RUN_USER=jellyclean
  pct exec "$CTID" -- bash -c "
    getent group $RUN_GID >/dev/null || groupadd -g $RUN_GID jellyclean
    id -u $RUN_UID >/dev/null 2>&1 || useradd -r -M -s /usr/sbin/nologin -u $RUN_UID -g $RUN_GID jellyclean
    getent passwd $RUN_UID | cut -d: -f1" > /tmp/jc_user.$$ || true
  RUN_USER=$(tr -d '\r\n' < /tmp/jc_user.$$); rm -f /tmp/jc_user.$$
  RUN_USER=${RUN_USER:-jellyclean}
fi
ok "Servis radi kao korisnik '$RUN_USER' ($RUN_UID:$RUN_GID)."

# --- NFS
if [[ "$MEDIA_MODE" == nfs ]]; then
  info "Montiram NFS…"
  for idx in "${!MEDIA_LABELS[@]}"; do
    pct exec "$CTID" -- bash -c "mkdir -p '${MEDIA_DST[$idx]}' && grep -q ' ${MEDIA_DST[$idx]} ' /etc/fstab || echo '$NFS_SERVER:${MEDIA_SRC[$idx]} ${MEDIA_DST[$idx]} nfs defaults,_netdev,nofail 0 0' >> /etc/fstab"
  done
  pct exec "$CTID" -- bash -c "systemctl daemon-reload; mount -a" || warn "mount -a javlja grešku – provjeri NFS export/dozvole."
fi

# --- provjera pisanja u medijske mape
for idx in "${!MEDIA_LABELS[@]}"; do
  d=${MEDIA_DST[$idx]}
  if pct exec "$CTID" -- bash -c "[ -d '$d' ] && setpriv --reuid=$RUN_UID --regid=$RUN_GID --clear-groups touch '$d/.jellyclean-write-test' 2>/dev/null && rm -f '$d/.jellyclean-write-test'"; then
    ok "${MEDIA_LABELS[$idx]}: pisanje radi."
  else
    warn "${MEDIA_LABELS[$idx]}: korisnik $RUN_UID nema pravo pisanja u $d – brisanje neće raditi dok se to ne riješi."
  fi
done

# --- kod
info "Instaliram JellyClean u $APP_DIR…"
case $SRC in
  local)
    tar -C "$LOCAL_SRC" -czf /tmp/jellyclean-src.$$.tgz --exclude=.git --exclude=data --exclude=__pycache__ .
    pct push "$CTID" /tmp/jellyclean-src.$$.tgz /tmp/jellyclean-src.tgz
    rm -f /tmp/jellyclean-src.$$.tgz
    pct exec "$CTID" -- bash -c "mkdir -p $APP_DIR && tar -xzf /tmp/jellyclean-src.tgz -C $APP_DIR && rm /tmp/jellyclean-src.tgz" ;;
  git)
    pct exec "$CTID" -- git clone -q --depth 1 "$REPO_URL" "$APP_DIR" ;;
  token)
    AUTH_URL=$(echo "$REPO_URL" | sed "s#https://#https://x-access-token:${GH_TOKEN}@#")
    pct exec "$CTID" -- bash -c "git clone -q --depth 1 '$AUTH_URL' $APP_DIR && git -C $APP_DIR remote set-url origin '$REPO_URL'" ;;
esac
pct exec "$CTID" -- test -f "$APP_DIR/app.py" || die "Instalacija koda nije uspjela."
ok "Kod instaliran."

# --- konfiguracija
media_env=""; for idx in "${!MEDIA_LABELS[@]}"; do media_env+="${media_env:+,}${MEDIA_LABELS[$idx]}=${MEDIA_DST[$idx]}"; done
jf_env="";    for idx in "${!JF_LABELS[@]}";    do jf_env+="${jf_env:+,}${JF_LABELS[$idx]}=${JF_DST[$idx]}"; done
ENVF=$(mktemp)
cat > "$ENVF" <<EOF
# JellyClean konfiguracija - nakon izmjene: systemctl restart jellyclean
PORT=$(envq "$PORT")
MEDIA_PATHS=$(envq "$media_env")
JELLYFIN_DIRS=$(envq "$jf_env")
JELLYFIN_URL=$(envq "$JF_URL")
JELLYFIN_API_KEY=$(envq "$JF_KEY")
JELLYFIN_USERS=$(envq "$JF_USERS")
JELLYFIN_VERIFY_TLS=$JF_TLS
PATH_MAP=$(envq "$PATH_MAP")
ALLOW_DELETE=$ALLOW_DELETE
TRASH_MODE=$TRASH
TRASH_DAYS=$(envq "$TRASH_DAYS")
SCAN_INTERVAL_HOURS=$(envq "$SCAN_H")
AUTH_USER=$(envq "$AUTH_USER")
AUTH_PASS=$(envq "$AUTH_PASS")
DATA_DIR=/var/lib/jellyclean
TZ=$(envq "$(cat /etc/timezone 2>/dev/null || timedatectl show -p Timezone --value 2>/dev/null || echo Europe/Zagreb)")
EOF
pct push "$CTID" "$ENVF" /etc/jellyclean.env --perms 600
rm -f "$ENVF"

SVCF=$(mktemp)
cat > "$SVCF" <<EOF
[Unit]
Description=JellyClean - pregled i čišćenje Jellyfin prostora
After=network-online.target remote-fs.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/jellyclean.env
User=$RUN_USER
Group=$RUN_GID
ExecStart=/usr/bin/python3 $APP_DIR/app.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
pct push "$CTID" "$SVCF" /etc/systemd/system/jellyclean.service
rm -f "$SVCF"

# skripta za nadogradnju unutar kontejnera
UPDF=$(mktemp)
cat > "$UPDF" <<EOF
#!/usr/bin/env bash
# Nadogradnja JellyCleana (za privatni repo: GH_TOKEN=xxx jellyclean-update)
set -e
cd $APP_DIR
if [ -d .git ]; then
  if [ -n "\${GH_TOKEN:-}" ]; then
    url=\$(git remote get-url origin)
    git pull -q "\${url/https:\/\//https://x-access-token:\$GH_TOKEN@}" HEAD
  else
    git pull -q --ff-only
  fi
else
  echo "Instalirano iz lokalnih datoteka - ponovno pokreni create-lxc.sh ili kopiraj datoteke ručno."; exit 1
fi
systemctl restart jellyclean
echo "JellyClean nadograđen: \$(git log -1 --format='%h %s')"
EOF
pct push "$CTID" "$UPDF" /usr/local/bin/jellyclean-update --perms 755
rm -f "$UPDF"

pct exec "$CTID" -- bash -c "mkdir -p /var/lib/jellyclean && chown $RUN_UID:$RUN_GID /var/lib/jellyclean && chown -R root:root $APP_DIR && systemctl daemon-reload && systemctl enable -q --now jellyclean"

# --- provjera
info "Čekam da se servis pokrene…"
CT_IP=""
for _ in $(seq 1 20); do
  CT_IP=$(pct exec "$CTID" -- bash -c "hostname -I | awk '{print \$1}'" 2>/dev/null | tr -d '\r\n' || true)
  if pct exec "$CTID" -- curl -fs -m 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then HEALTH=1; break; fi
  HEALTH=0; sleep 1
done
pct set "$CTID" --description "JellyClean - pregled i čišćenje Jellyfin prostora
http://${CT_IP:-<ip>}:$PORT" >/dev/null 2>&1 || true

echo
if [[ "${HEALTH:-0}" == 1 ]]; then ok "${C_B}JellyClean radi!${C_0}"; else warn "Servis se nije javio – pogledaj: pct exec $CTID -- journalctl -u jellyclean -n 50"; fi
cat <<EOF

  ${C_B}Sučelje:${C_0}       http://${CT_IP:-<ip>}:$PORT
  ${C_B}Prijava:${C_0}       ${AUTH_USER:-(bez lozinke)}${AUTH_USER:+ / $AUTH_PASS}
  ${C_B}Konfiguracija:${C_0} pct exec $CTID -- nano /etc/jellyclean.env  (pa systemctl restart jellyclean)
  ${C_B}Logovi:${C_0}        pct exec $CTID -- journalctl -u jellyclean -f
  ${C_B}Nadogradnja:${C_0}   pct exec $CTID -- jellyclean-update$([[ $SRC == token ]] && echo "   (privatni repo: GH_TOKEN=xxx jellyclean-update)")

EOF
[[ -n "$AUTH_USER" ]] && warn "Zapiši lozinku – nalazi se i u /etc/jellyclean.env unutar kontejnera."
if [[ "$ALLOW_DELETE" == true ]]; then
  warn "Ako su filmovi/serije praćeni u Radarru/Sonarru, isključi praćenje prije brisanja ili će se ponovno preuzeti."
fi
