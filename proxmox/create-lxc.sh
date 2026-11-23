#!/usr/bin/env bash
# ============================================================================
#  JellyClean - kreiranje Proxmox LXC kontejnera s automatskim otkrivanjem
#
#  Pokreni na Proxmox HOSTU kao root:
#     bash -c "$(curl -fsSL https://raw.githubusercontent.com/moordrak/JellyClean/main/proxmox/create-lxc.sh)"
#  ili iz kloniranog repozitorija:
#     bash proxmox/create-lxc.sh
#
#  Što se otkriva automatski (sve uz pojašnjenje i tvoje odobrenje):
#   • NFS exporti na NAS-u            (samo čitanje popisa, ništa se ne dodaje u Proxmox)
#   • Jellyfin biblioteke i njihove putanje (preko API-ja)
#   • koja mapa na NAS-u odgovara kojoj biblioteci (provjera na stvarnim datotekama)
#   • PATH_MAP, nazivi mapa, UID/GID vlasnika medija
#   • može li servis pisati (brisati) u svaku mapu
#   • Jellyfin config mapa na VM-u (docker inspect), SSH ključ za čišćenje cachea/logova/metadata
#  NFS se montira UNUTAR privilegiranog LXC-a (fstab u kontejneru), ne na hostu.
# ============================================================================
set -Eeuo pipefail

REPO_URL_DEFAULT="https://github.com/moordrak/JellyClean.git"
APP_DIR="/opt/jellyclean"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /tmp)"
LOCAL_SRC="$(cd "$SCRIPT_DIR/.." 2>/dev/null && pwd || echo /nonexistent)"
TITLE="JellyClean LXC"
NONINTERACTIVE="${NONINTERACTIVE:-0}"
TMPD=$(mktemp -d /tmp/jellyclean-lxc.XXXXXX)
CT_CREATED=0
CTID=""

# ---------------------------------------------------------------- izgled ----
if [[ -t 1 ]]; then
  C_OK=$'\e[32m'; C_ERR=$'\e[31m'; C_INF=$'\e[36m'; C_WRN=$'\e[33m'; C_B=$'\e[1m'; C_0=$'\e[0m'
else
  C_OK=""; C_ERR=""; C_INF=""; C_WRN=""; C_B=""; C_0=""
fi
info() { echo "${C_INF}➜${C_0} $*"; }
ok()   { echo "${C_OK}✔${C_0} $*"; }
warn() { echo "${C_WRN}!${C_0} $*"; }
die() {
  echo "${C_ERR}✘ $*${C_0}" >&2
  if (( CT_CREATED )); then
    echo "  Kontejner $CTID je kreiran. Pregled: pct enter $CTID   |   brisanje: pct stop $CTID; pct destroy $CTID" >&2
  fi
  exit 1
}
trap 'die "Greška u liniji $LINENO: $BASH_COMMAND"' ERR
trap 'rm -rf "$TMPD"' EXIT

# ---- Korisničko sučelje ----------------------------------------------------
# Zadano: whiptail dijaloški prozori (kao Proxmox helper skripte).
# Obična konzola: NO_WHIPTAIL=1 bash create-lxc.sh  (ili kad whiptail nije dostupan).
# Kad se skripta pokreće kao  curl … | bash  stdin nije terminal, pa unos čitamo iz /dev/tty.
if [[ "$NONINTERACTIVE" != "1" && ! -t 0 && -r /dev/tty ]] && { : < /dev/tty; } 2>/dev/null; then exec 3</dev/tty; else exec 3<&0; fi
UI=console
if [[ "$NONINTERACTIVE" != "1" && "${NO_WHIPTAIL:-0}" != "1" ]] && command -v whiptail >/dev/null && [[ -t 3 && -t 2 ]]; then UI=wt; fi
HR="${C_INF}────────────────────────────────────────────────────────────────────${C_0}"
STEP_TXT=""; STEP_N=7

banner() {
  local P=$'\e[38;5;141m' Cc=$'\e[38;5;45m' M=$'\e[38;5;245m' Z=$'\e[0m'
  [[ -t 1 ]] || { P=""; Cc=""; M=""; Z=""; }
  printf '\n%s    ╦┌─┐┬  ┬ ┬ ┬%s╔═╗┬  ┌─┐┌─┐┌┐┌%s\n' "$P" "$Cc" "$Z"
  printf '%s    ║├┤ │  │ └┬┘%s║  │  ├┤ ├─┤│││%s\n' "$P" "$Cc" "$Z"
  printf '%s   ╚╝└─┘┴─┘┴─┘┴ %s╚═╝┴─┘└─┘┴ ┴┘└┘%s\n' "$P" "$Cc" "$Z"
  printf '%s   pregled i čišćenje Jellyfin prostora · instalacija u Proxmox LXC%s\n' "$M" "$Z"
  [[ -n "$STEP_TXT" ]] && printf '\n   %s%s%s\n' "$C_B" "$STEP_TXT" "$C_0"
  echo
}
# step BROJ "naziv"  - oznaka koraka (u whiptailu na vrhu ekrana, u konzoli kao naslov)
step() {
  STEP_TXT="Korak $1/$STEP_N · $2"
  if [[ "$UI" == console && "$NONINTERACTIVE" != 1 ]]; then
    local i bar=""; for ((i = 1; i <= STEP_N; i++)); do (( i <= $1 )) && bar+="━━━━" || bar+="┄┄┄┄"; done
    printf '\n%s %s%s%s\n' "$HR" "$C_B" "$STEP_TXT" "$C_0"; printf '   %s%s%s\n' "$C_INF" "$bar" "$C_0"
  fi
}

# ---------------------------------------------------------------- konzola ---
_read() { local __v=$1 s=${2:-}; local x; if [[ -n "$s" ]]; then IFS= read -r -s x <&3 || true; echo; else IFS= read -r x <&3 || true; fi; printf -v "$__v" '%s' "$x"; }
_q() { printf '\n%b%b%b\n' "$C_B" "$1" "$C_0"; }

# --------------------------------------------------------------- whiptail ---
# dims "tekst" dodatni_redovi  -> postavlja WH, WW, WSCROLL
dims() {
  local t lines cols nl wid
  t=$(printf '%b' "$1")
  lines=$(tput lines 2>/dev/null || echo 24); cols=$(tput cols 2>/dev/null || echo 80)
  nl=$(printf '%s\n' "$t" | wc -l); wid=$(printf '%s\n' "$t" | wc -L)
  WW=$(( wid + 8 )); (( WW < 64 )) && WW=64; (( WW > cols - 4 )) && WW=$(( cols - 4 ))
  WH=$(( nl + ${2:-7} )); WSCROLL=()
  if (( WH > lines - 3 )); then WH=$(( lines - 3 )); WSCROLL=(--scrolltext); fi
}
wt() { whiptail --backtitle "JellyClean · instalacija u Proxmox LXC${STEP_TXT:+    ·    $STEP_TXT}" --title " JellyClean " "$@" 5>&1 1>&2 2>&5 <&3; }
wcancel() {
  if whiptail --backtitle "JellyClean" --title " Prekid " --yes-button "Prekini" --no-button "Nastavi" --defaultno \
       --yesno "Prekinuti instalaciju?$( (( CT_CREATED )) && printf '\n\nKontejner %s je već kreiran.' "$CTID")" 10 60 <&3; then
    die "Prekinuto."
  fi
}

# ------------------------------------------------------- zajednički upiti ---
# ask VAR "pitanje" "default"
ask() {
  local __var=$1 q=$2 def=${3:-} ans
  if [[ "$NONINTERACTIVE" == "1" ]]; then printf -v "$__var" '%s' "$def"; return; fi
  if [[ "$UI" == wt ]]; then
    dims "$q" 9
    while ! ans=$(wt --ok-button "Dalje" --cancel-button "Odustani" --inputbox "$q" "$WH" "$WW" "$def"); do wcancel; done
  else
    _q "$q"; printf '  > %s' "${def:+[$def] }"; _read ans; ans=${ans:-$def}
  fi
  printf -v "$__var" '%s' "$ans"
}
# ask_secret VAR "pitanje" ["default"] - unos se ne prikazuje; razmaci i novi redovi se uklanjaju
ask_secret() {
  local __var=$1 q=$2 def=${3:-} ans
  if [[ "$NONINTERACTIVE" == "1" ]]; then printf -v "$__var" '%s' "$def"; return; fi
  if [[ "$UI" == wt ]]; then
    local qq="$q${def:+\n\n(prazno = generiraj)}"
    dims "$qq" 9
    while ! ans=$(wt --ok-button "Dalje" --cancel-button "Odustani" --passwordbox "$qq" "$WH" "$WW"); do wcancel; done
  else
    _q "$q"; printf '  > %s' "${def:+(Enter = generiraj) }"; _read ans s
  fi
  ans=$(printf '%s' "$ans" | tr -d '[:space:]')
  [[ -n "$ans" && "$UI" == console ]] && printf '  %b✔%b primljeno (%d znakova)\n' "$C_OK" "$C_0" "${#ans}"
  printf -v "$__var" '%s' "${ans:-$def}"
}
# yesno "pitanje" default(y|n)
yesno() {
  local q=$1 def=${2:-y} ans rc
  if [[ "$NONINTERACTIVE" == "1" ]]; then [[ "$def" == y ]]; return; fi
  if [[ "$UI" == wt ]]; then
    dims "$q" 7
    while :; do
      rc=0
      if [[ "$def" == y ]]; then wt --yes-button "Da" --no-button "Ne" "${WSCROLL[@]}" --yesno "$q" "$WH" "$WW" || rc=$?
      else wt --yes-button "Da" --no-button "Ne" --defaultno "${WSCROLL[@]}" --yesno "$q" "$WH" "$WW" || rc=$?; fi
      case $rc in 0) return 0 ;; 1) return 1 ;; *) wcancel ;; esac
    done
  fi
  while :; do
    _q "$q"
    printf '  > [%s] ' "$( [[ $def == y ]] && echo D/n || echo d/N )"; _read ans; ans=${ans:-$def}
    case $ans in [yYdD]*) return 0 ;; [nN]*) return 1 ;; esac
    echo "  Odgovori s d (da) ili n (ne)."
  done
}
# menu VAR "pitanje" default tag opis tag opis ...
menu() {
  local __var=$1 q=$2 def=$3; shift 3
  local a=("$@") i n ans defn=1
  if [[ "$NONINTERACTIVE" == "1" ]]; then printf -v "$__var" '%s' "$def"; return; fi
  if [[ "$UI" == wt ]]; then
    local items=() cnt=$(( ${#a[@]} / 2 )) lh
    for ((i = 0; i < ${#a[@]}; i += 2)); do items+=("${a[i]}" "${a[i+1]:- }"); done
    dims "$q" $(( cnt + 9 )); lh=$(( WH - $(printf '%b\n' "$q" | wc -l) - 8 )); (( lh < 3 )) && lh=3; (( lh > cnt )) && lh=$cnt
    while ! ans=$(wt --ok-button "Odaberi" --cancel-button "Odustani" --default-item "$def" --menu "$q" "$WH" "$WW" "$lh" "${items[@]}"); do wcancel; done
    printf -v "$__var" '%s' "$ans"; return
  fi
  _q "$q"
  for ((i = 0; i < ${#a[@]}; i += 2)); do
    n=$(( i / 2 + 1 )); [[ "${a[i]}" == "$def" ]] && defn=$n
    if [[ -n "${a[i+1]}" ]]; then printf '   %2d) %s\n' "$n" "${a[i+1]}"; else printf '   %2d) %s\n' "$n" "${a[i]}"; fi
  done
  while :; do
    printf '  > [%s] ' "$defn"; _read ans; ans=${ans:-$defn}
    if [[ "$ans" =~ ^[0-9]+$ ]] && (( ans >= 1 && ans <= ${#a[@]} / 2 )); then printf -v "$__var" '%s' "${a[(ans-1)*2]}"; return; fi
    for ((i = 0; i < ${#a[@]}; i += 2)); do [[ "${a[i]}" == "$ans" ]] && { printf -v "$__var" '%s' "$ans"; return; }; done
    echo "  Upiši broj od 1 do $(( ${#a[@]} / 2 ))."
  done
}
# checklist VAR "pitanje" tag opis ON|OFF ...  -> VAR = odabrani tagovi (razmakom)
checklist() {
  local __var=$1 q=$2; shift 2
  local a=("$@") i n ans defn="" out="" t
  for ((i = 0; i < ${#a[@]}; i += 3)); do [[ ${a[i+2]} == ON ]] && defn+="$(( i / 3 + 1 )) "; done
  defn=${defn% }
  if [[ "$NONINTERACTIVE" == "1" ]]; then
    for t in $defn; do out+="${a[(t-1)*3]} "; done; printf -v "$__var" '%s' "${out% }"; return
  fi
  if [[ "$UI" == wt ]]; then
    local items=() cnt=$(( ${#a[@]} / 3 )) lh qq="$q\n\n(razmak = označi/odznači, Enter = potvrdi)"
    for ((i = 0; i < ${#a[@]}; i += 3)); do items+=("${a[i]}" "${a[i+1]:- }" "${a[i+2]}"); done
    dims "$qq" $(( cnt + 9 )); lh=$(( WH - $(printf '%b\n' "$qq" | wc -l) - 8 )); (( lh < 3 )) && lh=3; (( lh > cnt )) && lh=$cnt
    while ! ans=$(wt --ok-button "Potvrdi" --cancel-button "Odustani" --separate-output --checklist "$qq" "$WH" "$WW" "$lh" "${items[@]}"); do wcancel; done
    out=""; while IFS= read -r t; do [[ -n "$t" ]] && out+="$t "; done <<< "$ans"
    printf -v "$__var" '%s' "${out% }"; return
  fi
  _q "$q"
  for ((i = 0; i < ${#a[@]}; i += 3)); do
    n=$(( i / 3 + 1 ))
    printf '   %2d) [%s] %s\n' "$n" "$([[ ${a[i+2]} == ON ]] && echo x || echo ' ')" "${a[i+1]:-${a[i]}}"
  done
  echo "  Upiši brojeve odvojene razmakom (npr. 1 3), 'sve' ili 'nista'. Enter = označeni [x]."
  while :; do
    printf '  > [%s] ' "${defn:-nista}"; _read ans; ans=${ans:-${defn:-nista}}
    ans=${ans//,/ }; out=""
    case ${ans,,} in sve|all) for ((i = 0; i < ${#a[@]}; i += 3)); do out+="${a[i]} "; done; break ;; nista|ništa|none|0) break ;; esac
    local bad=0
    for t in $ans; do
      if [[ "$t" =~ ^[0-9]+$ ]] && (( t >= 1 && t <= ${#a[@]} / 3 )); then out+="${a[(t-1)*3]} "; else bad=1; fi
    done
    (( bad )) && { echo "  Neispravan unos. Upiši brojeve od 1 do $(( ${#a[@]} / 3 ))."; continue; }
    break
  done
  printf -v "$__var" '%s' "${out% }"
}
msg() {
  if [[ "$NONINTERACTIVE" == "1" ]]; then printf '\n%b\n' "$1"; return; fi
  if [[ "$UI" == wt ]]; then dims "$1" 7; wt --ok-button "Dalje" "${WSCROLL[@]}" --msgbox "$1" "$WH" "$WW" || true; return; fi
  printf '\n%s\n%b\n%s\n' "$HR" "$1" "$HR"
}
pause() { [[ "$NONINTERACTIVE" == "1" || "$UI" == wt ]] && return; printf '  Pritisni Enter za nastavak… '; _read _; }
jf_auth() { printf 'Authorization: MediaBrowser Client="JellyClean", Device="Proxmox", DeviceId="jellyclean-setup", Version="1.0", Token="%s"' "$1"; }
envq() { local v=${1//\\/\\\\}; v=${v//\"/\\\"}; printf '"%s"' "$v"; }
slugify() { echo "$1" | iconv -f utf-8 -t ascii//TRANSLIT 2>/dev/null | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/^-//;s/-$//'; }
ctx() { pct exec "$CTID" -- bash -c "$1"; }

# ------------------------------------------------------------ provjere -----
[[ $EUID -eq 0 ]] || die "Pokreni kao root na Proxmox hostu."
command -v pct >/dev/null && command -v pveam >/dev/null || die "Ovo nije Proxmox VE host (nema pct/pveam)."

[[ "$NONINTERACTIVE" != 1 ]] && { clear 2>/dev/null || true; }
banner
[[ "$UI" == wt ]] && sleep 1
msg "Dobro došao u instalaciju JellyCleana!

Skripta će AUTOMATSKI:
 1. predložiti postavke kontejnera (možeš ih prihvatiti ili promijeniti)
 2. pronaći NFS shareove na tvom NAS-u
 3. kreirati PRIVILEGED kontejner i montirati shareove UNUTAR njega
    (na Proxmox host se ništa ne dodaje)
 4. pitati Jellyfin koje biblioteke ima i na kojim putanjama
 5. pronaći koja mapa na NAS-u odgovara kojoj biblioteci - provjerom
    stvarnih datoteka iz Jellyfina
 6. složiti PATH_MAP, prepoznati vlasnika datoteka (UID/GID) i
    provjeriti može li servis brisati

Prije svakog koraka koji nešto mijenja vidjet ćeš prijedlog s
pojašnjenjem i tražit će se tvoje odobrenje."

# ===================================================== 1. KONTEJNER ======
step 1 "Kontejner"
NEXTID=$(pvesh get /cluster/nextid 2>/dev/null || echo 120)

mapfile -t TSTORES < <(pvesm status -content vztmpl 2>/dev/null | awk 'NR>1 && $3=="active"{print $1}')
(( ${#TSTORES[@]} )) || die "Nema aktivnog storagea za LXC predloške (content: vztmpl)."
mapfile -t RSTORES < <(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active"{printf "%s %d\n",$1,$6/1048576}' | sort -k2 -nr)
(( ${#RSTORES[@]} )) || die "Nema aktivnog storagea za LXC diskove (content: rootdir)."
mapfile -t BRIDGES < <(ip -o link show type bridge 2>/dev/null | awk -F': ' '{print $2}' | sed 's/@.*//' | grep -E '^vmbr' || true)
(( ${#BRIDGES[@]} )) || BRIDGES=(vmbr0)

CTID=$NEXTID; CT_HOST=jellyclean; TSTORE=${TSTORES[0]}; OS_CHOICE=debian-12
RSTORE=${RSTORES[0]%% *}; DISK=4; CORES=2; RAM=1024; SWAP=512
BRIDGE=${BRIDGES[0]}; VLAN=""; NETMODE=dhcp; DNS=""; IPCIDR=""; GW=""

if ! yesno "Prijedlog postavki kontejnera:

  ID:         $CTID
  Hostname:   $CT_HOST
  OS:         Debian 12
  Disk:       $DISK GB na '$RSTORE' (storage s najviše slobodnog mjesta: ${RSTORES[0]##* } GB)
  CPU / RAM:  $CORES jezgre / $RAM MB (+$SWAP MB swap)
  Mreža:      $BRIDGE, DHCP
  Vrsta:      privileged (potrebno za NFS unutar kontejnera)

JellyClean je lagan servis (Python, bez baze), pa je ovo dovoljno
i za veliku biblioteku.

Prihvatiti prijedlog?" y; then
  while :; do
    ask CTID "ID kontejnera" "$CTID"
    [[ "$CTID" =~ ^[0-9]+$ ]] && (( CTID >= 100 )) || { msg "ID mora biti broj >= 100."; continue; }
    if pct status "$CTID" &>/dev/null || qm status "$CTID" &>/dev/null; then msg "ID $CTID je već zauzet."; continue; fi
    break
  done
  ask CT_HOST "Hostname" "$CT_HOST"
  menu OS_CHOICE "Operativni sustav" "$OS_CHOICE" "debian-12" "Debian 12 (preporučeno)" "debian-13" "Debian 13" "ubuntu-24.04" "Ubuntu 24.04 LTS"
  if (( ${#TSTORES[@]} > 1 )); then args=(); for s in "${TSTORES[@]}"; do args+=("$s" "predlošci"); done; menu TSTORE "Storage za predložak" "$TSTORE" "${args[@]}"; fi
  args=(); for s in "${RSTORES[@]}"; do args+=("${s%% *}" "slobodno ${s##* } GB"); done
  menu RSTORE "Storage za disk kontejnera" "$RSTORE" "${args[@]}"
  ask DISK "Disk (GB)" "$DISK"; ask CORES "CPU jezgre" "$CORES"; ask RAM "RAM (MB)" "$RAM"; ask SWAP "Swap (MB)" "$SWAP"
  [[ "$DISK$CORES$RAM$SWAP" =~ ^[0-9]+$ ]] || die "Disk, jezgre, RAM i swap moraju biti brojevi."
  if (( ${#BRIDGES[@]} > 1 )); then args=(); for b in "${BRIDGES[@]}"; do args+=("$b" "bridge"); done; menu BRIDGE "Mrežni bridge" "$BRIDGE" "${args[@]}"; fi
  ask VLAN "VLAN tag (prazno = bez)" ""
  [[ -z "$VLAN" || "$VLAN" =~ ^[0-9]+$ ]] || die "VLAN mora biti broj."
  menu NETMODE "IP adresa" "dhcp" "dhcp" "DHCP" "static" "Statička"
  if [[ "$NETMODE" == static ]]; then
    while :; do
      ask IPCIDR "IP s maskom (npr. 192.168.1.50/24)" ""
      [[ "$IPCIDR" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$ ]] && break; msg "Neispravan format."
    done
    ask GW "Gateway" "$(echo "$IPCIDR" | cut -d/ -f1 | awk -F. '{print $1"."$2"."$3".1"}')"
    ask DNS "DNS (prazno = kao host)" ""
  fi
else
  if pct status "$CTID" &>/dev/null || qm status "$CTID" &>/dev/null; then die "ID $CTID je zauzet."; fi
fi
if [[ "$NETMODE" == static ]]; then NET0="name=eth0,bridge=$BRIDGE,ip=$IPCIDR,gw=$GW"; else NET0="name=eth0,bridge=$BRIDGE,ip=dhcp"; fi
[[ -n "$VLAN" ]] && NET0+=",tag=$VLAN"

# ===================================================== 2. NAS / NFS ======
step 2 "NFS shareovi na NAS-u"
NAS=""
while [[ -z "$NAS" ]]; do ask NAS "IP adresa ili ime NAS-a (NFS server)" ""; done

info "Tražim NFS exporte na $NAS…"
EXPORTS=()
if out=$(pvesm scan nfs "$NAS" 2>/dev/null) && [[ -n "$out" ]]; then
  mapfile -t EXPORTS < <(echo "$out" | awk '{print $1}' | grep '^/' | sort -u)
elif command -v showmount >/dev/null && out=$(showmount -e --no-headers "$NAS" 2>/dev/null) && [[ -n "$out" ]]; then
  mapfile -t EXPORTS < <(echo "$out" | awk '{print $1}' | grep '^/' | sort -u)
fi
SEL_EXPORTS=()
if (( ${#EXPORTS[@]} )); then
  ok "Pronađeno ${#EXPORTS[@]} exporta."
  args=(); any=0
  for e in "${EXPORTS[@]}"; do
    st=OFF; [[ "${e,,}" =~ (media|video|movie|film|tv|serij|series|show|anime|jellyfin|plex|emby|data) ]] && { st=ON; any=1; }
    args+=("$e" "" "$st")
  done
  if (( ! any )); then for ((i = 2; i < ${#args[@]}; i += 3)); do args[i]=ON; done; fi
  checklist SEL "NAS $NAS dijeli ove NFS exporte (popis je samo pročitan, ništa nije montirano).

Označi one na kojima su filmovi i serije. Unaprijed su označeni oni
čije ime upućuje na medije. Svaki odabrani export bit će montiran
UNUTAR kontejnera u /mnt/nas/<ime>." "${args[@]}"
  read -r -a SEL_EXPORTS <<< "$SEL"
else
  warn "Popis exporta nije dostupan (NAS možda nudi samo NFSv4 ili blokira upite)."
  msg "Popis NFS exporta s NAS-a $NAS nije dostupan.

To je normalno za NAS-ove koji nude samo NFSv4. Upiši exporte ručno
(npr. /volume1/media na Synologyju, /mnt/pool/media na TrueNAS-u)."
  while :; do
    ask e "NFS export (prazno = gotovo)" ""
    [[ -z "$e" ]] && break
    SEL_EXPORTS+=("$e")
  done
fi
(( ${#SEL_EXPORTS[@]} )) || die "Nije odabran nijedan NFS export."

menu NFS_VER "Verzija NFS protokola" "auto" \
  "auto" "Automatski (preporučeno)" "4.1" "NFSv4.1" "4" "NFSv4.0" "3" "NFSv3"
NFS_OPTS="defaults,_netdev,nofail,x-systemd.mount-timeout=30"
[[ "$NFS_VER" != auto ]] && NFS_OPTS+=",vers=$NFS_VER"

declare -A USED_SLUG=()
MNT_SRC=(); MNT_DST=()
for e in "${SEL_EXPORTS[@]}"; do
  s=$(slugify "$(basename "$e")"); s=${s:-share}; base=$s; n=2
  while [[ -n "${USED_SLUG[$s]:-}" ]]; do s="$base-$n"; n=$((n + 1)); done
  USED_SLUG[$s]=1
  MNT_SRC+=("$NAS:$e"); MNT_DST+=("/mnt/nas/$s")
done

# =================================================== 3. JELLYFIN API =====
step 3 "Jellyfin API"
JF_URL=""; JF_KEY=""; JF_USERS=""; JF_TLS=true
if yesno "Povezati s Jellyfinom?

Preporučeno. Uz API ključ skripta sama otkriva biblioteke i njihove
mape, a JellyClean prikazuje tko je što pogledao.

API ključ: Jellyfin → Nadzorna ploča → API ključevi → +" y; then
  while :; do
    ask JF_URL "Jellyfin URL (npr. http://192.168.1.20:8096 ili https://jellyfin.home.int)" ""
    JF_URL=$(printf '%s' "$JF_URL" | tr -d '[:space:]'); JF_URL=${JF_URL%/}
    [[ -n "$JF_URL" && ! "$JF_URL" =~ ^https?:// ]] && JF_URL="http://$JF_URL"
    [[ "$JF_URL" =~ ^https?://[^/]+ ]] && break; msg "URL mora počinjati s http:// ili https://"
  done
  while :; do
    ask_secret JF_KEY "Jellyfin API ključ" ""
    [[ -n "$JF_KEY" ]] || { yesno "Bez ključa nema automatskog otkrivanja. Nastaviti bez Jellyfina?" n && { JF_URL=""; break; }; continue; }
    code=$(curl -sk -o /dev/null -w '%{http_code}' -m 8 -H "$(jf_auth "$JF_KEY")" "$JF_URL/System/Info" 2>/dev/null || true); code=${code:-000}
    case $code in
      200) ok "Jellyfin API ključ radi."; break ;;
      401|403) msg "Jellyfin odbija ključ (HTTP $code).

Provjeri:
  • je li ključ kopiran cijeli (Nadzorna ploča → API ključevi; ključ ima 32 znaka)
  • je li URL Jellyfina točan; ako ide preko nginxa, prosljeđuje li nginx header Authorization" ;;
      *) yesno "Jellyfin na $JF_URL nije dostupan s Proxmox hosta (HTTP $code).\n\nMožda je dostupan iz kontejnera. Nastaviti s ovim podacima?" y && break ;;
    esac
  done
  if [[ -n "$JF_URL" && "$JF_URL" == https://* ]]; then
    if ! curl -s -o /dev/null -m 5 "$JF_URL" 2>/dev/null; then
      yesno "TLS certifikat Jellyfina nije provjeren (samopotpisani certifikat?).\n\nIsključiti provjeru certifikata?" y && JF_TLS=false
    fi
  fi
  [[ -n "$JF_URL" ]] && ask JF_USERS "Koji se korisnici broje za 'pogledali svi'? (imena odvojena zarezom, prazno = svi)" ""
fi

# ============================================ 3b. JELLYFIN CONFIG (SSH) ===
step 4 "Jellyfin config (SSH)"
SSH_ON=0; SSH_HOST=""; SSH_USER=root; SSH_PORT=22
if yesno "Čišćenje Jellyfin config mape (cache, slike, logovi, siročad u metadata)?

Jellyfin radi na drugom računalu (npr. u Dockeru u VM-u), pa mu JellyClean
pristupa preko SSH-a. Skripta će:
  • u kontejneru napraviti SSH ključ (samo za JellyClean)
  • postaviti ga na Jellyfin računalo (upišeš lozinku jednom ili ga dodaš ručno)
  • sama pronaći gdje je Jellyfin config (docker inspect ili standardna instalacija)
  • ako SSH korisnik nema prava na Jellyfin mape, ponuditi ograničeni pomoćni
    program (uz sudo lozinku, koja se koristi samo tijekom postavljanja)" y; then
  SSH_ON=1
  def_host=""
  [[ -n "$JF_URL" ]] && def_host=$(echo "$JF_URL" | sed -E 's#^https?://##; s#[:/].*$##')
  ask SSH_HOST "IP adresa ili ime računala na kojem radi Jellyfin (VM s Dockerom)

Ako Jellyfinu pristupaš preko nginxa, ovdje upiši adresu VM-a, ne nginxa." "$def_host"
  ask SSH_USER "SSH korisnik na tom računalu (npr. root ili tvoj korisnik; root ovlasti nisu nužne)" "$SSH_USER"
  ask SSH_PORT "SSH port" "$SSH_PORT"
  [[ -n "$SSH_HOST" && "$SSH_PORT" =~ ^[0-9]+$ ]] || { warn "Neispravni SSH podaci - preskačem."; SSH_ON=0; }
fi

# ===================================================== 4. PONAŠANJE ======
step 5 "Postavke servisa"
ALLOW_DELETE=true; TRASH=true; TRASH_DAYS=7; SCAN_H=6; PORT=8095; AUTH_USER=admin
AUTH_PASS=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16 || true)
if ! yesno "Prijedlog ponašanja servisa:

  • brisanje iz sučelja:   DA, preko kante
  • kanta:                 obrisano se premješta u .jellyclean-trash i može se
                           vratiti; trajno se briše nakon $TRASH_DAYS dana
  • ponovno skeniranje:    svakih $SCAN_H sati
  • port:                  $PORT
  • prijava:               $AUTH_USER / generirana lozinka (prikazat će se na kraju)

Prihvatiti?" y; then
  if yesno "Dopustiti brisanje iz sučelja? (Ne = samo pregled)" y; then
    if yesno "Koristiti kantu?" y; then ask TRASH_DAYS "Automatski isprazniti kantu nakon X dana (0 = nikad)" "7"; else TRASH=false; fi
  else ALLOW_DELETE=false; fi
  ask SCAN_H "Ponovno skeniranje svakih X sati (0 = isključeno)" "$SCAN_H"
  ask PORT "Port" "$PORT"
  ask AUTH_USER "Korisničko ime (prazno = bez lozinke)" "$AUTH_USER"
  [[ -n "$AUTH_USER" ]] && ask_secret AUTH_PASS "Lozinka" "$AUTH_PASS"
fi

# ===================================================== 5. IZVOR KODA =====
SRC=git; REPO_URL=$REPO_URL_DEFAULT; GH_TOKEN=""
if [[ -f "$LOCAL_SRC/app.py" && -d "$LOCAL_SRC/static" ]]; then SRC=local; fi
if [[ "$SRC" == git ]]; then
  if ! git ls-remote "$REPO_URL" HEAD &>/dev/null; then
    menu SRC "Repozitorij $REPO_URL nije javno dostupan." "token" \
      "token" "Kloniraj uz GitHub token (privatni repozitorij)" "git" "Pokušaj svejedno"
    if [[ "$SRC" == token ]]; then ask_secret GH_TOKEN "GitHub token (fine-grained, Contents: read)" ""; [[ -n "$GH_TOKEN" ]] || die "Token je obavezan."; fi
  fi
fi

# ======================================================== SAŽETAK ========
summary="Kontejner:  $CTID ($CT_HOST), privileged, $OS_CHOICE
Resursi:    $CORES CPU, $RAM MB RAM, $DISK GB na $RSTORE
Mreža:      $BRIDGE${VLAN:+ VLAN $VLAN}, $([[ $NETMODE == static ]] && echo "$IPCIDR gw $GW" || echo DHCP)
NFS (montira se unutar kontejnera, NFS: $NFS_VER):"
for i in "${!MNT_SRC[@]}"; do summary+=$'\n'"  ${MNT_SRC[$i]}  →  ${MNT_DST[$i]}"; done
summary+="
Jellyfin:   ${JF_URL:-nije povezan}
Config:     $([[ $SSH_ON == 1 ]] && echo "SSH $SSH_USER@$SSH_HOST:$SSH_PORT" || echo "ne")
Brisanje:   $ALLOW_DELETE, kanta: $TRASH ($TRASH_DAYS d), skeniranje ${SCAN_H}h, port $PORT
Izvor koda: $SRC

Nakon kreiranja slijedi automatsko otkrivanje mapa, a prijedlog
ćeš opet morati odobriti."
yesno "$summary\n\nKreirati kontejner?" y || die "Prekinuto - ništa nije kreirano."

# ======================================================= KREIRANJE =======
step 6 "Kreiranje i otkrivanje"
[[ "$UI" == wt ]] && { clear; banner; }
info "Tražim predložak $OS_CHOICE…"
pveam update >/dev/null 2>&1 || warn "pveam update nije uspio, koristim lokalni popis."
case $OS_CHOICE in
  debian-12) PAT='^debian-12-standard_.*amd64' ;;
  debian-13) PAT='^debian-13-standard_.*amd64' ;;
  ubuntu-24.04) PAT='^ubuntu-24\.04-standard_.*amd64' ;;
  *) die "Nepoznat OS: $OS_CHOICE" ;;
esac
TEMPLATE=$(pveam list "$TSTORE" 2>/dev/null | awk 'NR>1{print $1}' | sed 's#.*/##' | grep -E "$PAT" | sort -V | tail -1 || true)
if [[ -z "$TEMPLATE" ]]; then
  TEMPLATE=$(pveam available --section system 2>/dev/null | awk '{print $2}' | grep -E "$PAT" | sort -V | tail -1 || true)
  [[ -n "$TEMPLATE" ]] || die "Predložak za $OS_CHOICE nije pronađen."
  info "Preuzimam $TEMPLATE…"
  pveam download "$TSTORE" "$TEMPLATE" >/dev/null
fi
ok "Predložak: $TEMPLATE"

create_args=(
  --hostname "$CT_HOST" --cores "$CORES" --memory "$RAM" --swap "$SWAP"
  --rootfs "$RSTORE:$DISK" --net0 "$NET0" --ostype "${OS_CHOICE%%-*}"
  --unprivileged 0 --features "nesting=1,mount=nfs" --onboot 1 --start 0
  --description "JellyClean - pregled i čišćenje Jellyfin prostora"
  --tags "jellyclean;media"
)
[[ -n "$DNS" ]] && create_args+=(--nameserver "$DNS")
info "Kreiram kontejner $CTID…"
pct create "$CTID" "$TSTORE:vztmpl/$TEMPLATE" "${create_args[@]}" >/dev/null
CT_CREATED=1
ok "Kontejner $CTID kreiran."

info "Pokrećem kontejner…"
pct start "$CTID"
for _ in $(seq 1 30); do ctx 'ping -c1 -W1 1.1.1.1 >/dev/null 2>&1' && break; sleep 2; done
ctx 'ping -c1 -W2 1.1.1.1 >/dev/null 2>&1' || die "Kontejner nema mrežu (provjeri bridge/VLAN/IP)."
ok "Mreža radi."

info "Instaliram pakete (python3, nfs-common, git, curl, ssh)…"
ctx "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get -y -qq upgrade >/dev/null && apt-get install -y -qq python3 nfs-common git curl ca-certificates openssh-client >/dev/null"
ok "Paketi instalirani."

# ------------------------------------------------------------ NFS mount ----
info "Montiram NFS unutar kontejnera…"
MOUNT_OK=(); MOUNT_FAIL=()
for i in "${!MNT_SRC[@]}"; do
  src=${MNT_SRC[$i]}; dst=${MNT_DST[$i]}
  ctx "mkdir -p '$dst'; grep -qF ' $dst ' /etc/fstab || echo '$src $dst nfs $NFS_OPTS 0 0' >> /etc/fstab"
  if ctx "mountpoint -q '$dst' || mount '$dst'" 2>"$TMPD/mnterr"; then
    MOUNT_OK+=("$dst"); ok "$src → $dst"
  else
    MOUNT_FAIL+=("$src ($(tr '\n' ' ' < "$TMPD/mnterr"))"); warn "$src nije montiran: $(cat "$TMPD/mnterr")"
  fi
done
if (( ${#MOUNT_FAIL[@]} )); then
  yesno "Neki exporti se nisu montirali:\n\n$(printf '  %s\n' "${MOUNT_FAIL[@]}")\n\nNajčešći uzrok: NAS ne dopušta IP adresu kontejnera (dodaj je u NFS dozvole na NAS-u)\nili kriva verzija NFS-a.\n\nNastaviti s onima koji rade?" n || die "Prekinuto."
fi
(( ${#MOUNT_OK[@]} )) || die "Nijedan NFS export nije montiran."

# ===================================================== OTKRIVANJE ========
cat > "$TMPD/discover.py" <<'PYEOF'
import collections, json, os, re, ssl, sys, urllib.parse, urllib.request
roots = [r for r in os.environ.get("ROOTS", "").split("\n") if r]
url = os.environ.get("JF_URL", "").rstrip("/")
key = os.environ.get("JF_KEY", "")
verify = os.environ.get("JF_VERIFY_TLS", "true") == "true"
SKIP = {"@eaDir", "#recycle", "#snapshot", "$RECYCLE.BIN", "lost+found", ".snapshot", "System Volume Information"}
MEDIA_RE = re.compile(r"movie|film|tv|serij|series|show|anime|cartoon|crtan|docu|dokument|kids|djec|4k|uhd|music|glazb|concert|koncert", re.I)
VIDEO = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".m2ts", ".mov", ".wmv", ".iso", ".webm", ".mpg"}

def out(*a):
    print("\t".join(str(x).replace("\t", " ").replace("\n", " ") for x in a), flush=True)

def jf(path, params=None):
    u = url + path + ("?" + urllib.parse.urlencode(params) if params else "")
    auth = f'MediaBrowser Client="JellyClean", Device="Proxmox", DeviceId="jellyclean-setup", Version="1.0", Token="{key}"'
    r = urllib.request.Request(u, headers={"Authorization": auth, "Accept": "application/json"})
    ctx = None if verify else ssl._create_unverified_context()
    with urllib.request.urlopen(r, timeout=60, context=ctx) as resp:
        return json.loads(resp.read() or b"null")

def subdirs(d):
    try:
        return sorted(e.path for e in os.scandir(d) if e.is_dir(follow_symlinks=False)
                      and e.name not in SKIP and not e.name.startswith(".")
                      and not e.name.endswith(".trickplay"))
    except OSError:
        return []

# Kandidati: mape do dubine 3, ali se ne ulazi u mapu s >150 podmapa (to je već biblioteka)
cands = []
for r in roots:
    level = [r]
    cands.append(r)
    for depth in range(3):
        nxt = []
        for d in level:
            sd = subdirs(d)
            if len(sd) > 150:
                continue
            nxt.extend(sd)
        cands.extend(nxt)
        level = nxt
for c in cands[:400]:
    out("CAND", c)

owners = collections.Counter()
def note_owner(p):
    try:
        st = os.stat(p)
        owners[(st.st_uid, st.st_gid)] += 1
    except OSError:
        pass

matched_dirs = []
if url and key:
    try:
        libs = jf("/Library/VirtualFolders") or []
    except Exception as e:
        out("ERR", f"Jellyfin API: {e}")
        libs = []
    for lib in libs:
        name = lib.get("Name") or "Biblioteka"
        ctype = lib.get("CollectionType") or "mixed"
        locs = lib.get("Locations") or []
        samples_all = []
        try:
            res = jf("/Items", {"ParentId": lib.get("ItemId", ""), "Recursive": "true", "Fields": "Path",
                                "IsFolder": "false", "Limit": 400, "EnableImages": "false",
                                "IncludeItemTypes": "Movie,Episode,Video,Audio,MusicVideo"})
            samples_all = [i.get("Path") for i in (res or {}).get("Items", []) if i.get("Path")]
        except Exception as e:
            out("ERR", f"{name}: {e}")
        for li, loc in enumerate(locs):
            loc_n = loc.rstrip("/")
            label = name if len(locs) == 1 else f"{name} {li + 1}"
            rels = [p[len(loc_n) + 1:] for p in samples_all if p.startswith(loc_n + "/")]
            if len(rels) > 8:
                step = len(rels) // 8
                rels = rels[::step][:8]
            best, best_hits, method = None, 0, "-"
            if rels:
                for c in cands:
                    if not os.path.exists(os.path.join(c, rels[0])):
                        continue
                    hits = sum(1 for r in rels if os.path.exists(os.path.join(c, r)))
                    better = hits > best_hits or (hits == best_hits and best and
                             os.path.basename(c).lower() == os.path.basename(loc_n).lower())
                    if better:
                        best, best_hits, method = c, hits, "datoteke"
            if not best:
                parts = [p.lower() for p in loc_n.split("/") if p]
                score_best = 0
                for c in cands:
                    cp = [p.lower() for p in c.split("/") if p]
                    k = 0
                    while k < min(len(parts), len(cp)) and parts[-1 - k] == cp[-1 - k]:
                        k += 1
                    if k > score_best:
                        best, score_best, method = c, k, "ime"
            if best and method == "datoteke":
                for r in rels:
                    note_owner(os.path.join(best, r))
            elif best:
                try:
                    for e in os.scandir(best):
                        note_owner(e.path)
                        if sum(owners.values()) > 30:
                            break
                except OSError:
                    pass
            if best:
                matched_dirs.append(best)
            out("LIB", label, ctype, loc_n, best or "-", len(rels), best_hits, method)
else:
    for c in cands:
        if c in roots:
            continue
        if MEDIA_RE.search(os.path.basename(c)):
            if not any(c.startswith(m + "/") for m in matched_dirs):
                matched_dirs.append(c)
                out("HEUR", os.path.basename(c), c)
                n = 0
                for dp, dn, fn in os.walk(c):
                    for f in fn:
                        if os.path.splitext(f)[1].lower() in VIDEO:
                            note_owner(os.path.join(dp, f)); n += 1
                    if n >= 10:
                        break
if owners:
    (u, g), cnt = owners.most_common(1)[0]
    out("OWNER", u, g, cnt, sum(owners.values()))
PYEOF

info "Otkrivam biblioteke i mape…"
pct push "$CTID" "$TMPD/discover.py" /root/jc-discover.py
ROOTS_STR=$(printf '%s\n' "${MOUNT_OK[@]}")
pct exec "$CTID" -- env ROOTS="$ROOTS_STR" JF_URL="$JF_URL" JF_KEY="$JF_KEY" JF_VERIFY_TLS="$JF_TLS" \
  python3 /root/jc-discover.py > "$TMPD/disc.tsv" 2>"$TMPD/disc.err" || warn "Otkrivanje javlja grešku: $(tail -1 "$TMPD/disc.err")"
ctx "rm -f /root/jc-discover.py" || true

L_LABEL=(); L_TYPE=(); L_JF=(); L_CT=(); L_N=(); L_HIT=(); L_METHOD=(); CANDS=(); OWN_U=""; OWN_G=""; OWN_C=0; OWN_T=0
while IFS=$'\t' read -r kind a b c d e f g; do
  case $kind in
    CAND) CANDS+=("$a") ;;
    LIB)  L_LABEL+=("$a"); L_TYPE+=("$b"); L_JF+=("$c"); L_CT+=("$d"); L_N+=("$e"); L_HIT+=("$f"); L_METHOD+=("$g") ;;
    HEUR) L_LABEL+=("$a"); L_TYPE+=("?"); L_JF+=("-"); L_CT+=("$b"); L_N+=(0); L_HIT+=(0); L_METHOD+=("naziv") ;;
    OWNER) OWN_U=$a; OWN_G=$b; OWN_C=$c; OWN_T=$d ;;
    ERR) warn "$a" ;;
  esac
done < "$TMPD/disc.tsv"

# ---- prijedlog mapiranja
args=(); expl=""
for i in "${!L_LABEL[@]}"; do
  if [[ "${L_CT[$i]}" != "-" ]]; then
    if [[ "${L_METHOD[$i]}" == datoteke ]]; then
      conf="✓ ${L_HIT[$i]}/${L_N[$i]} datoteka"; st=ON
      (( L_HIT[i] * 2 < L_N[i] )) && { conf="? samo ${L_HIT[$i]}/${L_N[$i]} datoteka"; st=OFF; }
    elif [[ "${L_METHOD[$i]}" == naziv ]]; then conf="po nazivu mape"; st=ON
    else conf="? samo po imenu (prazna biblioteka?)"; st=OFF; fi
    [[ "${L_TYPE[$i]}" =~ ^(books|playlists|boxsets)$ ]] && st=OFF
    args+=("$i" "${L_LABEL[$i]}: ${L_CT[$i]}  [$conf]" "$st")
  else
    args+=("$i" "${L_LABEL[$i]}: NIJE PRONAĐENO (Jellyfin: ${L_JF[$i]})" OFF)
  fi
done

SEL=""
if (( ${#L_LABEL[@]} == 0 )); then
  msg "Automatsko otkrivanje nije pronašlo biblioteke$([[ -z "$JF_URL" ]] && echo " (Jellyfin nije povezan, a nijedna mapa nema ime koje upućuje na medije)").\n\nOdabrat ćeš mape ručno."
else
  if [[ -n "$JF_URL" ]]; then
    expl="Jellyfin ima ${#L_LABEL[@]} biblioteka. Za svaku je skripta uzela nekoliko stvarnih
datoteka iz Jellyfina i potražila ih na montiranim NFS shareovima.

  ✓ x/y   pronađeno x od y datoteka: pouzdano (unaprijed označeno)
  ?       pogođeno samo po imenu mape ili djelomično: provjeri
  NIJE PRONAĐENO   biblioteka nije na odabranim exportima

Označene biblioteke JellyClean će skenirati i u njima moći brisati.
Ako označiš nepronađenu, moći ćeš joj ručno odabrati mapu."
  else
    expl="Jellyfin nije povezan, pa su predložene mape čije ime upućuje na medije.
Označene mape JellyClean će skenirati i u njima moći brisati."
  fi
  checklist SEL "$expl" "${args[@]}"
fi

pick_dir() {   # pick_dir VAR "pitanje"  (prazno = preskoči)
  local __v=$1 q=$2 r margs=()
  for c in "${CANDS[@]}"; do margs+=("$c" ""); done
  margs+=("ručno" "upiši putanju" "preskoči" "")
  while :; do
    menu r "$q" "ručno" "${margs[@]}"
    [[ "$r" == "preskoči" ]] && { r=""; break; }
    [[ "$r" == "ručno" ]] && ask r "Putanja unutar kontejnera" "${MOUNT_OK[0]}/"
    r=${r%/}
    [[ -n "$r" ]] && ctx "[ -d '$r' ]" && break
    msg "Mapa '$r' ne postoji u kontejneru. Odaberi ponovno."
  done
  printf -v "$__v" '%s' "$r"
}

FIN_LABEL=(); FIN_CT=(); FIN_JF=()
for i in $SEL; do
  ct=${L_CT[$i]}
  [[ "$ct" == "-" ]] && pick_dir ct "Odaberi mapu za '${L_LABEL[$i]}' (Jellyfin: ${L_JF[$i]})"
  [[ -n "$ct" && "$ct" != "-" ]] || continue
  FIN_LABEL+=("${L_LABEL[$i]}"); FIN_CT+=("${ct%/}"); FIN_JF+=("${L_JF[$i]}")
done
while :; do
  if (( ${#FIN_LABEL[@]} )); then
    yesno "Dodati još neku mapu ručno?\n\nTrenutno odabrano:\n$(for i in "${!FIN_LABEL[@]}"; do echo "  ${FIN_LABEL[$i]}: ${FIN_CT[$i]}"; done)" n || break
  fi
  pick_dir ct "Odaberi mapu s medijima"
  [[ -n "$ct" ]] || { (( ${#FIN_LABEL[@]} )) && break || continue; }
  ask lbl "Naziv za $ct" "$(basename "$ct")"
  FIN_LABEL+=("$lbl"); FIN_CT+=("${ct%/}"); FIN_JF+=("-")
done

# ---- PATH_MAP
pm=(); pm_txt=""
for i in "${!FIN_LABEL[@]}"; do
  if [[ "${FIN_JF[$i]}" != "-" && "${FIN_JF[$i]}" != "${FIN_CT[$i]}" ]]; then
    pm+=("${FIN_JF[$i]}=${FIN_CT[$i]}"); pm_txt+="  ${FIN_JF[$i]}  →  ${FIN_CT[$i]}"$'\n'
  fi
done
PATH_MAP=$(IFS=';'; echo "${pm[*]:-}")

# ---- vlasnik
RUN_UID=${OWN_U:-1000}; RUN_GID=${OWN_G:-1000}
own_expl="Vlasnik datoteka: UID $RUN_UID / GID $RUN_GID"
if [[ -n "$OWN_U" ]]; then own_expl+=" (vlasnik $OWN_C od $OWN_T provjerenih datoteka)"; else own_expl+=" (nije otkriveno - zadano 1000)"; fi
[[ "$RUN_UID" == 65534 ]] && own_expl+="\n\n! UID 65534 = 'nobody': NAS vjerojatno radi squash svih korisnika. Brisanje će raditi samo ako NAS dopušta pisanje tom korisniku."

yesno "Prijedlog konfiguracije (automatski otkriveno):

Medijske mape:
$(for i in "${!FIN_LABEL[@]}"; do echo "  ${FIN_LABEL[$i]}: ${FIN_CT[$i]}"; done)

PATH_MAP (Jellyfin putanja → putanja u kontejneru):
${pm_txt:-  (nije potreban)}
$own_expl
JellyClean će raditi kao taj korisnik, pa na NAS-u ima ista prava
kao Jellyfin i *arr: može brisati njihove datoteke, a ništa više.

Prihvatiti?" y || {
  ask RUN_UID "UID korisnika s kojim JellyClean radi" "$RUN_UID"
  ask RUN_GID "GID" "$RUN_GID"
  [[ "$RUN_UID$RUN_GID" =~ ^[0-9]+$ ]] || die "UID i GID moraju biti brojevi."
  ask PATH_MAP "PATH_MAP (jellyfin=kontejner;...)" "$PATH_MAP"
}

# ---- korisnik i provjera pisanja
if [[ "$RUN_UID" == 0 ]]; then RUN_USER=root; else
  RUN_USER=$(ctx "getent group $RUN_GID >/dev/null || groupadd -g $RUN_GID jellyclean
    id -u $RUN_UID >/dev/null 2>&1 || useradd -r -M -s /usr/sbin/nologin -u $RUN_UID -g $RUN_GID jellyclean
    getent passwd $RUN_UID | cut -d: -f1" | tr -d '\r\n')
  RUN_USER=${RUN_USER:-jellyclean}
fi
WFAIL=()
for i in "${!FIN_CT[@]}"; do
  d=${FIN_CT[$i]}
  if ctx "setpriv --reuid=$RUN_UID --regid=$RUN_GID --clear-groups touch '$d/.jellyclean-write-test' 2>/dev/null && rm -f '$d/.jellyclean-write-test'"; then
    ok "${FIN_LABEL[$i]}: brisanje moguće."
  else
    WFAIL+=("${FIN_LABEL[$i]} ($d)"); warn "${FIN_LABEL[$i]}: UID $RUN_UID ne može pisati u $d"
  fi
done
if (( ${#WFAIL[@]} )); then
  yesno "Korisnik $RUN_USER (UID $RUN_UID) ne može pisati u:\n\n$(printf '  %s\n' "${WFAIL[@]}")\n\nPregled će raditi, ali brisanje u tim mapama neće.\nRješenje je na NAS-u: dozvoli pisanje korisniku $RUN_UID za IP kontejnera,\nili provjeri squash postavke NFS exporta.\n\nNastaviti?" y || die "Prekinuto."
fi

# ========================================================= INSTALACIJA ===
step 7 "Instalacija"
info "Instaliram JellyClean u $APP_DIR…"
case $SRC in
  local)
    tar -C "$LOCAL_SRC" -czf "$TMPD/src.tgz" --exclude=.git --exclude=data --exclude=__pycache__ .
    pct push "$CTID" "$TMPD/src.tgz" /tmp/jellyclean-src.tgz
    ctx "mkdir -p $APP_DIR && tar -xzf /tmp/jellyclean-src.tgz -C $APP_DIR && rm /tmp/jellyclean-src.tgz" ;;
  git)
    pct exec "$CTID" -- git clone -q --depth 1 "$REPO_URL" "$APP_DIR" ;;
  token)
    AUTH_URL=${REPO_URL/https:\/\//https://x-access-token:${GH_TOKEN}@}
    pct exec "$CTID" -- bash -c "git clone -q --depth 1 '$AUTH_URL' $APP_DIR && git -C $APP_DIR remote set-url origin '$REPO_URL'" ;;
esac
pct exec "$CTID" -- test -f "$APP_DIR/app.py" || die "Instalacija koda nije uspjela."
ok "Kod instaliran."
cat > "$TMPD/upd" <<'UPDEOF'
#!/usr/bin/env bash
# JellyClean - nadogradnja na najnoviju verziju (pokreni unutar kontejnera kao root)
#   jellyclean-update               javni repozitorij
#   GH_TOKEN=xxx jellyclean-update  privatni repozitorij
# Radi i za instalaciju iz gita (git pull) i bez gita (preuzima najnoviji release).
set -euo pipefail
APP=/opt/jellyclean
REPO=moordrak/JellyClean
ver() { grep -oP '^APP_VERSION = "\K[^"]+' "$APP/app.py" 2>/dev/null || echo "?"; }
[ -f "$APP/app.py" ] || { echo "Nema $APP/app.py - JellyClean nije instaliran u ovom kontejneru."; exit 1; }
OLD=$(ver)
AUTH=()
[ -n "${GH_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer $GH_TOKEN")

if [ -d "$APP/.git" ]; then
  echo "➜ git pull…"
  cd "$APP"
  if [ -n "${GH_TOKEN:-}" ]; then
    url=$(git remote get-url origin)
    git pull -q --ff-only "${url/https:\/\//https://x-access-token:$GH_TOKEN@}" HEAD
  else
    git pull -q --ff-only
  fi
else
  echo "➜ preuzimam najnoviji release s GitHuba…"
  TAG=$(curl -fsSL "${AUTH[@]}" "https://api.github.com/repos/$REPO/releases/latest" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])') \
    || { echo "Ne mogu dohvatiti najnoviji release (privatni repozitorij? pokreni s GH_TOKEN=...)"; exit 1; }
  TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
  curl -fsSL "${AUTH[@]}" -o "$TMP/src.tgz" "https://api.github.com/repos/$REPO/tarball/$TAG"
  tar -xzf "$TMP/src.tgz" -C "$TMP"
  SRC=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)
  [ -f "$SRC/app.py" ] || { echo "Preuzeta arhiva ne sadrži app.py"; exit 1; }
  cp -a "$SRC"/. "$APP"/
fi
chown -R root:root "$APP"
systemctl restart jellyclean
sleep 2
if systemctl is-active -q jellyclean; then
  echo "✔ JellyClean nadograđen: $OLD → $(ver)"
else
  echo "✘ Servis se nije pokrenuo - journalctl -u jellyclean -n 50"; exit 1
fi
UPDEOF
pct push "$CTID" "$TMPD/upd" /usr/local/bin/jellyclean-update --perms 755

# ================================================ JELLYFIN CONFIG (SSH) ===
JF_SSH_SUDO=false; JF_SSH_HELPER=""; JFC_CONFIG=""; JFC_CACHE=""; JFC_LOG=""; JFC_DATA=""; JFC_META=""
KEY=/var/lib/jellyclean/ssh/id_ed25519
HELPER_PATH=/usr/local/sbin/jellyclean-helper
SSH_OPTS="-p $SSH_PORT -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/var/lib/jellyclean/ssh/known_hosts -o ConnectTimeout=10 -o LogLevel=ERROR"
SUDOPW=""; PRIV=""
cssh() { pct exec "$CTID" -- bash -c "ssh $SSH_OPTS -o BatchMode=yes '$SSH_USER@$SSH_HOST' $1"; }
# rsh FILE MODE  -> izvrši lokalnu sh skriptu na Jellyfin računalu (plain | sudo | sudopw)
rsh() {
  local f=$1 m=$2 rc
  case $m in
    plain)  cp "$f" "$TMPD/payload"; rc="sh -s" ;;
    sudo)   cp "$f" "$TMPD/payload"; rc="sudo -n sh -s" ;;
    sudopw) { printf '%s\n' "$SUDOPW"; cat "$f"; } > "$TMPD/payload"; rc="sudo -k -S -p '' sh -s" ;;
  esac
  chmod 600 "$TMPD/payload"
  pct push "$CTID" "$TMPD/payload" /root/jc-payload --perms 600; rm -f "$TMPD/payload"
  pct exec "$CTID" -- bash -c "ssh $SSH_OPTS -o BatchMode=yes '$SSH_USER@$SSH_HOST' \"$rc\" < /root/jc-payload 2>/root/jc-payload.err; r=\$?; rm -f /root/jc-payload; exit \$r"
}
# root_mode -> postavi PRIV na sudo ili sudopw (pita lozinku jednom); vrati 1 ako nije moguće
root_mode() {
  [[ -n "$PRIV" ]] && return 0
  if cssh "'sudo -n true'" 2>/dev/null; then PRIV=sudo; return 0; fi
  msg "Za ovaj korak trebaju root ovlasti na $SSH_HOST.

Korisnik $SSH_USER nema sudo bez lozinke, pa skripta treba njegovu sudo
lozinku (onu koju upisuješ za 'sudo -i'). Lozinka se koristi SAMO sada,
tijekom postavljanja, i nigdje se ne sprema."
  local t
  for t in 1 2 3; do
    ask_secret SUDOPW "Sudo lozinka za $SSH_USER@$SSH_HOST (prazno = preskoči)" ""
    [[ -n "$SUDOPW" ]] || return 1
    printf 'echo JC_ROOT_OK $(id -u)\n' > "$TMPD/t.sh"
    local r; r=$(rsh "$TMPD/t.sh" sudopw 2>/dev/null || true)
    if [[ "$r" == *"JC_ROOT_OK 0"* ]]; then PRIV=sudopw; ok "sudo lozinka prihvaćena."; return 0; fi
    warn "sudo lozinka nije prihvaćena."
  done
  SUDOPW=""; return 1
}

if (( SSH_ON )); then
  info "Pripremam SSH ključ za pristup Jellyfin računalu…"
  ctx "mkdir -p /var/lib/jellyclean/ssh && chmod 700 /var/lib/jellyclean/ssh && [ -f $KEY ] || ssh-keygen -q -t ed25519 -N '' -C 'jellyclean@$CT_HOST' -f $KEY"
  PUBKEY=$(ctx "cat $KEY.pub" | tr -d '\r')
  if cssh true 2>/dev/null; then
    ok "SSH pristup već radi."
  else
    menu KEYMODE "Kako postaviti SSH ključ na $SSH_USER@$SSH_HOST?

Ključ je napravljen samo za JellyClean. Javni dio treba dodati u
~$SSH_USER/.ssh/authorized_keys na Jellyfin računalu." "lozinka" \
      "lozinka" "Automatski - upisat ću SSH lozinku za $SSH_USER@$SSH_HOST jednom (ssh-copy-id)" \
      "rucno" "Ručno - prikaži javni ključ, sam ću ga dodati" \
      "preskoci" "Preskoči - bez čišćenja Jellyfin configa"
    case $KEYMODE in
      lozinka)
        echo; info "Upiši SSH lozinku za $SSH_USER@$SSH_HOST (ne sprema se):"
        pct exec "$CTID" -- ssh-copy-id -i "$KEY.pub" -p "$SSH_PORT" -o StrictHostKeyChecking=accept-new \
          -o UserKnownHostsFile=/var/lib/jellyclean/ssh/known_hosts "$SSH_USER@$SSH_HOST" <&3 || warn "ssh-copy-id nije uspio." ;;
      rucno)
        msg "Dodaj ovaj red u ~$SSH_USER/.ssh/authorized_keys na $SSH_HOST:

$PUBKEY

Npr. na Jellyfin računalu kao $SSH_USER:
  mkdir -p ~/.ssh && echo '$PUBKEY' >> ~/.ssh/authorized_keys"
        pause ;;
      *) SSH_ON=0 ;;
    esac
    if (( SSH_ON )); then
      until cssh true 2>/dev/null; do
        yesno "SSH spajanje na $SSH_USER@$SSH_HOST još ne radi.\n\nJavni ključ:\n$PUBKEY\n\nPokušati ponovno? (Ne = preskoči čišćenje Jellyfin configa)" y || { SSH_ON=0; break; }
      done
    fi
  fi
fi

if (( SSH_ON )); then
  ok "SSH pristup na $SSH_USER@$SSH_HOST radi."
  [[ "$SSH_USER" == root ]] && PRIV=plain

  # ---- 1) gdje je Jellyfin config
  cat > "$TMPD/jfdetect.sh" <<'DETEOF'
found=0
if command -v docker >/dev/null 2>&1; then
  if ! docker ps >/dev/null 2>&1; then echo "DOCKERPERM"; fi
  for id in $(docker ps -a --format '{{.ID}} {{.Image}} {{.Names}}' 2>/dev/null | grep -i jellyfin | awk '{print $1}'); do
    name=$(docker inspect -f '{{.Name}}' "$id" | sed 's#^/##'); img=$(docker inspect -f '{{.Config.Image}}' "$id")
    cfg=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/config"}}{{.Source}}{{end}}{{end}}' "$id")
    cch=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/cache"}}{{.Source}}{{end}}{{end}}' "$id")
    puid=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$id" | sed -n 's/^PUID=//p' | head -1)
    [ -n "$cfg" ] || continue
    found=1
    echo "SRC docker:$name ($img)"
    echo "CONFIG $cfg"
    # linuxserver: /config/data/metadata, službeni image: /config/metadata
    if [ -d "$cfg/data/metadata" ]; then echo "DATA $cfg/data"; elif [ -d "$cfg/metadata" ]; then echo "DATA $cfg"; else echo "DATA $cfg/data"; fi
    if [ -n "$cch" ]; then echo "CACHE $cch"; else echo "CACHE $cfg/cache"; fi
    echo "LOG $cfg/log"
    [ -n "$puid" ] && echo "PUID $puid"
    break
  done
fi
if [ $found = 0 ] && [ -d /var/lib/jellyfin ]; then
  echo "SRC paket (systemd)"; echo "CONFIG /var/lib/jellyfin"; echo "DATA /var/lib/jellyfin"
  echo "CACHE /var/cache/jellyfin"; echo "LOG /var/log/jellyfin"; found=1
fi
[ $found = 1 ] || echo "NONE"
DETEOF
  info "Tražim Jellyfin config na $SSH_HOST…"
  DET=$(rsh "$TMPD/jfdetect.sh" "${PRIV:-plain}" 2>/dev/null || true)
  if [[ "$DET" == *DOCKERPERM* && "$DET" != *CONFIG* && -z "$PRIV" ]]; then
    info "$SSH_USER nema pristup Dockeru - za otkrivanje trebaju root ovlasti."
    if root_mode; then DET=$(rsh "$TMPD/jfdetect.sh" "$PRIV" 2>/dev/null || true); fi
  fi
  JSRC=$(echo "$DET" | sed -n 's/^SRC //p' | head -1)
  JFC_CONFIG=$(echo "$DET" | sed -n 's/^CONFIG //p' | head -1)
  JFC_DATA=$(echo "$DET" | sed -n 's/^DATA //p' | head -1)
  JFC_CACHE=$(echo "$DET" | sed -n 's/^CACHE //p' | head -1)
  JFC_LOG=$(echo "$DET" | sed -n 's/^LOG //p' | head -1)
  JPUID=$(echo "$DET" | sed -n 's/^PUID //p' | head -1)
  [[ -n "$JFC_DATA" ]] && JFC_META="$JFC_DATA/metadata"
  if [[ -n "$JFC_CONFIG" ]]; then
    yesno "Pronađen Jellyfin: $JSRC

  config:    $JFC_CONFIG
  cache:     $JFC_CACHE      (cache/images, transcodes…)
  logovi:    $JFC_LOG
  metadata:  $JFC_META

Putanje su na računalu $SSH_HOST. JellyClean će ovdje mjeriti
zauzeće i, kad ti to odobriš u sučelju, brisati cache, stare
logove i siročad u metadata. Baza (data/*.db) se nikad ne dira.

Prihvatiti?" y || JFC_CONFIG=""
  fi
  if [[ -z "$JFC_CONFIG" ]]; then
    [[ -z "$JSRC" ]] && msg "Jellyfin config nije automatski pronađen na $SSH_HOST\n(nema Docker kontejnera s 'jellyfin' u imenu, ni /var/lib/jellyfin).\n\nUpiši putanje ručno."
    ask JFC_CONFIG "Putanja do Jellyfin /config mape na $SSH_HOST (prazno = preskoči)" "${JFC_CONFIG:-/opt/jellyfin/config}"
    if [[ -n "$JFC_CONFIG" ]]; then
      ask JFC_CACHE "Cache mapa" "${JFC_CACHE:-$JFC_CONFIG/cache}"
      ask JFC_LOG "Log mapa" "${JFC_LOG:-$JFC_CONFIG/log}"
      ask JFC_DATA "Data mapa (u njoj je metadata)" "${JFC_DATA:-$JFC_CONFIG/data}"
      JFC_META="$JFC_DATA/metadata"
    else SSH_ON=0; fi
  fi
fi

if (( SSH_ON )); then
  # ---- 2) način pristupa: izravno, ili ograničeni helper preko sudo
  q() { printf "'%s'" "${1//\'/\'\\\'\'}"; }
  cat > "$TMPD/access.sh" <<EOF
C=$(q "$JFC_CONFIG"); CACHE=$(q "$JFC_CACHE"); LOG=$(q "$JFC_LOG"); META=$(q "$JFC_META")
[ -d "\$C" ] || { echo "NOCFG"; exit 0; }
bad=\$(find "\$CACHE" "\$LOG" "\$META/library" -type d \( ! -writable -o ! -readable \) -print -quit 2>/dev/null)
nr=\$( [ -r "\$C" ] && echo 1 || echo 0 )
if [ -z "\$bad" ] && [ "\$nr" = 1 ]; then echo "DIRECT_OK"; else echo "DIRECT_NO \${bad:-\$C} \$(stat -c '%U(%u)' "\${bad:-\$C}" 2>/dev/null)"; fi
echo "ME \$(id -un)(\$(id -u))"
EOF
  ACC=$(rsh "$TMPD/access.sh" plain 2>/dev/null || true)
  if [[ "$ACC" == *NOCFG* && "$SSH_USER" != root ]]; then ACC="DIRECT_NO $JFC_CONFIG ?"; fi
  if [[ "$SSH_USER" == root ]]; then
    ok "Pristup kao root - nisu potrebne dodatne ovlasti."
  elif [[ "$ACC" == *DIRECT_OK* ]]; then
    ok "$SSH_USER već ima pravo čitanja i brisanja u Jellyfin cache/log/metadata mapama - sudo nije potreban."
  else
    BADP=$(echo "$ACC" | sed -n 's/^DIRECT_NO //p')
    ME=$(echo "$ACC" | sed -n 's/^ME //p')
    msg "Korisnik ${ME:-$SSH_USER} nema prava na sve Jellyfin mape
(npr. ${BADP:-$JFC_CONFIG}${JPUID:+; Jellyfin kontejner radi s PUID=$JPUID}).

Prijedlog: ograničeni pomoćni program
  • skripta jednom (uz tvoju sudo lozinku) postavi na $SSH_HOST:
      $HELPER_PATH                (vlasnik root, nitko ga ne može mijenjati)
      /etc/sudoers.d/jellyclean   (pravilo, provjereno s visudo)
  • pravilo dopušta da $SSH_USER bez lozinke pokreće SAMO taj program
  • program zna samo: izmjeriti mape i obrisati cache, stare logove
    i siročad u metadata, i to samo u ovim putanjama:
      $JFC_CACHE
      $JFC_LOG
      $JFC_META/library
  • ne može pokrenuti ništa drugo, niti dirati bazu ili postavke

Tako JellyClean (i njegov SSH ključ) nema potpuni root na tom računalu."
    menu ACCMODE "Kako dati JellyCleanu pristup Jellyfin mapama?" "helper" \
      "helper" "Ograničeni pomoćni program + sudoers pravilo (preporučeno)" \
      "preskoci" "Preskoči - bez čišćenja Jellyfin configa"
    if [[ "$ACCMODE" == helper ]] && root_mode; then
      info "Postavljam $HELPER_PATH na $SSH_HOST…"
      pct exec "$CTID" -- env JF_CONFIG_PATH="$JFC_CONFIG" JF_CACHE_PATH="$JFC_CACHE" JF_LOG_PATH="$JFC_LOG" \
        JF_DATA_PATH="$JFC_DATA" JF_METADATA_PATH="$JFC_META" python3 "$APP_DIR/app.py" --helper-script > "$TMPD/helper.sh"
      grep -q '^main "\$@"' "$TMPD/helper.sh" || die "Generiranje pomoćnog programa nije uspjelo."
      {
        echo 'set -e'
        echo "cat > $HELPER_PATH.new <<'JC_HELPER_EOF'"
        cat "$TMPD/helper.sh"
        echo 'JC_HELPER_EOF'
        echo "chown root:root $HELPER_PATH.new && chmod 755 $HELPER_PATH.new && mv -f $HELPER_PATH.new $HELPER_PATH"
        echo "printf '%s\n' '# JellyClean: $SSH_USER smije bez lozinke pokretati SAMO jellyclean-helper' '$SSH_USER ALL=(root) NOPASSWD: $HELPER_PATH' > /etc/sudoers.d/jellyclean.new"
        echo 'chmod 440 /etc/sudoers.d/jellyclean.new'
        echo 'if visudo -cf /etc/sudoers.d/jellyclean.new >/dev/null; then mv -f /etc/sudoers.d/jellyclean.new /etc/sudoers.d/jellyclean; else rm -f /etc/sudoers.d/jellyclean.new; echo SUDOERS_FAIL; exit 1; fi'
        echo 'echo HELPER_OK'
      } > "$TMPD/install-helper.sh"
      HOUT=$(rsh "$TMPD/install-helper.sh" "$PRIV" 2>/dev/null || true)
      SUDOPW=""
      HCHK=$(cssh "'sudo -n $HELPER_PATH status'" 2>/dev/null || true)
      if [[ "$HOUT" == *HELPER_OK* && "$HCHK" == *"HOST "* ]]; then
        JF_SSH_HELPER=$HELPER_PATH
        ok "Pomoćni program radi: $SSH_USER@$SSH_HOST → sudo -n $HELPER_PATH"
      else
        warn "Postavljanje pomoćnog programa nije uspjelo${HOUT:+ ($HOUT)}."
        SSH_ON=0
      fi
    else
      [[ "$ACCMODE" == helper ]] && warn "Bez root ovlasti nije moguće postaviti pomoćni program."
      SSH_ON=0
    fi
  fi
  SUDOPW=""
fi
if (( ! SSH_ON )); then SSH_HOST=""; JFC_CONFIG=""; JF_SSH_HELPER=""; fi
ctx "chown -R $RUN_UID:$RUN_GID /var/lib/jellyclean 2>/dev/null || true; rm -f /root/jc-payload /root/jc-payload.err"


media_env=""; for i in "${!FIN_LABEL[@]}"; do lbl=${FIN_LABEL[$i]//[,=;]/ }; media_env+="${media_env:+,}${lbl}=${FIN_CT[$i]}"; done
TZV=$(cat /etc/timezone 2>/dev/null || timedatectl show -p Timezone --value 2>/dev/null || echo Europe/Zagreb)
cat > "$TMPD/env" <<EOF
# JellyClean konfiguracija (generirano $(date '+%Y-%m-%d %H:%M')) - nakon izmjene: systemctl restart jellyclean
PORT=$(envq "$PORT")
MEDIA_PATHS=$(envq "$media_env")
JELLYFIN_DIRS=""
JF_SSH_HOST=$(envq "$SSH_HOST")
JF_SSH_USER=$(envq "$SSH_USER")
JF_SSH_PORT=$(envq "$SSH_PORT")
JF_SSH_KEY=/var/lib/jellyclean/ssh/id_ed25519
JF_SSH_SUDO=$JF_SSH_SUDO
JF_SSH_HELPER=$(envq "$JF_SSH_HELPER")
JF_CONFIG_PATH=$(envq "$JFC_CONFIG")
JF_CACHE_PATH=$(envq "$JFC_CACHE")
JF_LOG_PATH=$(envq "$JFC_LOG")
JF_DATA_PATH=$(envq "$JFC_DATA")
JF_METADATA_PATH=$(envq "$JFC_META")
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
TZ=$(envq "$TZV")
EOF
pct push "$CTID" "$TMPD/env" /etc/jellyclean.env --perms 600

cat > "$TMPD/svc" <<EOF
[Unit]
Description=JellyClean - pregled i čišćenje Jellyfin prostora
After=network-online.target remote-fs.target
Wants=network-online.target remote-fs.target

[Service]
EnvironmentFile=/etc/jellyclean.env
# NFS mora biti montiran prije pokretanja (nofail u fstabu ne čeka)
ExecStartPre=+/bin/sh -c 'mount -a -t nfs,nfs4 2>/dev/null || true'
User=$RUN_USER
Group=$RUN_GID
ExecStart=/usr/bin/python3 $APP_DIR/app.py
Restart=on-failure
RestartSec=10
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
pct push "$CTID" "$TMPD/svc" /etc/systemd/system/jellyclean.service


ctx "mkdir -p /var/lib/jellyclean && chown -R $RUN_UID:$RUN_GID /var/lib/jellyclean && chown -R root:root $APP_DIR && systemctl daemon-reload && systemctl enable -q --now jellyclean"

info "Čekam da se servis pokrene…"
CT_IP=""; HEALTH=0
for _ in $(seq 1 20); do
  CT_IP=$(ctx "hostname -I | awk '{print \$1}'" 2>/dev/null | tr -d '\r\n' || true)
  if pct exec "$CTID" -- curl -fs -m 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then HEALTH=1; break; fi
  sleep 1
done
pct set "$CTID" --description "JellyClean - pregled i čišćenje Jellyfin prostora
http://${CT_IP:-<ip>}:$PORT
NAS: $NAS" >/dev/null 2>&1 || true

if [[ "$UI" == wt ]]; then
  msg "$( (( HEALTH )) && echo "✔ JellyClean radi!" || echo "! Servis se nije javio - provjeri: pct exec $CTID -- journalctl -u jellyclean -n 50")

Sučelje:   http://${CT_IP:-<ip>}:$PORT
Prijava:   ${AUTH_USER:-(bez lozinke)}${AUTH_USER:+ / $AUTH_PASS}
Mape:      $media_env
Config:    $([[ -n "$JFC_CONFIG" ]] && echo "$SSH_USER@$SSH_HOST:$JFC_CONFIG" || echo "(nije podešeno)")

Isti sažetak ostaje ispisan u konzoli nakon zatvaranja prozora."
  clear; STEP_TXT="Gotovo"; banner
fi
echo
if (( HEALTH )); then ok "${C_B}JellyClean radi!${C_0}"; else warn "Servis se nije javio – pct exec $CTID -- journalctl -u jellyclean -n 50"; fi
cat <<EOF

  ${C_B}Sučelje:${C_0}       http://${CT_IP:-<ip>}:$PORT
  ${C_B}Prijava:${C_0}       ${AUTH_USER:-(bez lozinke)}${AUTH_USER:+ / $AUTH_PASS}
  ${C_B}Mape:${C_0}          $media_env
  ${C_B}PATH_MAP:${C_0}      ${PATH_MAP:-(nije potreban)}
  ${C_B}Korisnik:${C_0}      $RUN_USER ($RUN_UID:$RUN_GID)
  ${C_B}Jellyfin config:${C_0} $([[ -n "$JFC_CONFIG" ]] && echo "$SSH_USER@$SSH_HOST:$JFC_CONFIG" || echo "(nije podešeno)")

  Konfiguracija:  pct exec $CTID -- nano /etc/jellyclean.env   (pa: systemctl restart jellyclean)
  NFS mountovi:   pct exec $CTID -- cat /etc/fstab
  Logovi:         pct exec $CTID -- journalctl -u jellyclean -f
  Nadogradnja:    pct exec $CTID -- /usr/local/bin/jellyclean-update

EOF
[[ -n "$AUTH_USER" ]] && warn "Zapiši lozinku (nalazi se i u /etc/jellyclean.env u kontejneru)."
[[ "$ALLOW_DELETE" == true ]] && warn "Filmove/serije praćene u Radarru/Sonarru isključi iz praćenja prije brisanja, inače će se ponovno preuzeti."
exit 0
