#!/usr/bin/env bash
# =============================================================================
#  jellyfin-docker2lxc.sh  v1.5.0
#
#  Migracija Jellyfina iz Docker kontejnera (npr. u VM-u na STAROM serveru)
#  u novi LXC na (novom) Proxmox hostu sa samostalnim (native, apt) Jellyfinom:
#    - grafički čarobnjak (whiptail) u stilu Proxmox community-scripts
#    - prenosi SVE postavke, korisnike, bazu, metapodatke, pluginove (preko SSH-a)
#    - hardversko transkodiranje: NVIDIA NVENC/NVDEC ili Intel QSV (iGPU)
#    - LXC na odabranom storageu (zadano: NVMe LVM-Thin)
#    - mediji s NAS-a preko NFS-a (na istim putanjama kao u Dockeru)
#    - opcija da stari i novi Jellyfin rade paralelno (novi Server ID)
#    - svaki odgovor se pamti – nakon greške ne treba sve ponovno upisivati
#
#  Pokreni NA (NOVOM) PROXMOX HOSTU kao root:
#     bash jellyfin-docker2lxc.sh                    # nova migracija / nastavak
#     bash jellyfin-docker2lxc.sh --resume           # nastavak nakon restarta hosta
#     bash jellyfin-docker2lxc.sh --text             # bez grafičkog sučelja
#     jellyfin-docker2lxc --api                      # pokreće JellyClean (pitanja u web sučelju)
#     bash jellyfin-docker2lxc.sh --sync-nvidia 123  # uskladi NVIDIA u LXC-u nakon
#                                                    # nadogradnje drajvera na hostu
#  Log: /root/jellyfin-migration/migration.log
# =============================================================================
set -Eeuo pipefail
shopt -s lastpipe

SCRIPT_VERSION="1.5.0"
SELF="$(readlink -f "$0")"
STATE_DIR="/root/jellyfin-migration"
ANSWERS="$STATE_DIR/answers.env"
LOG="$STATE_DIR/migration.log"
TARBALL="$STATE_DIR/jellyfin-config.tar.gz"
SSH_CTL="/root/.ssh/jfmig-%r@%h:%p"
LEGACY_BRANCH=580   # zadnja grana NVIDIA drajvera s podrškom za Maxwell/Pascal (GTX 9xx/10xx)
mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"

# ============================================================================= UI
# fd 4 = terminal (poruke, spinner) – radi i unutar $(...)
# --api: pokreće ga JellyClean preko jellyclean-pve-agenta. Pitanja idu u $API_DIR/question.json,
#        odgovori stižu u answer.json, napredak se piše u progress.log.
API_MODE=0; API_DIR="$STATE_DIR/api"
if [[ " $* " == *" --api "* ]]; then
  API_MODE=1; mkdir -p "$API_DIR"; chmod 700 "$API_DIR"
  exec 4>>"$API_DIR/progress.log"
elif ( : >/dev/tty ) 2>/dev/null; then exec 4>/dev/tty; else exec 4>&1; fi
if [[ -t 4 ]]; then
  B=$'\e[1m'; D=$'\e[2m'; R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; C=$'\e[36m'; N=$'\e[0m'
else B=""; D=""; R=""; G=""; Y=""; C=""; N=""; fi
USE_TUI=1
[[ " $* " == *" --text "* || ${JF_NO_TUI:-0} == 1 ]] && USE_TUI=0
command -v whiptail >/dev/null || USE_TUI=0
[[ -t 4 ]] || USE_TUI=0
(( API_MODE )) && USE_TUI=0
WT_BT="Jellyfin  ·  Docker ➜ Proxmox LXC  ·  v$SCRIPT_VERSION"
CUR_SECTION="Jellyfin"
FRAMES=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
SPIN_PID=""

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }
run() { log "RUN: $*"; "$@" >>"$LOG" 2>&1; }

stop_spin() {
  if [[ -n $SPIN_PID ]]; then
    kill "$SPIN_PID" 2>/dev/null || true
    wait "$SPIN_PID" 2>/dev/null || true
    SPIN_PID=""
    printf '\r\e[K' >&4
    tput cnorm >&4 2>/dev/null || true
  fi
}
msg_info() {
  stop_spin; log "INFO $*"
  if [[ -t 4 ]]; then
    local m=$*
    tput civis >&4 2>/dev/null || true
    ( trap 'exit 0' TERM; trap - ERR; i=0
      while :; do printf '\r\e[K %s%s%s %s' "$C" "${FRAMES[i % 10]}" "$N" "$m" >&4; i=$((i + 1)); sleep 0.1; done ) &
    SPIN_PID=$!
  else
    echo " … $*" >&4
  fi
}
spin_wait() {   # spin_wait PID "poruka" [datoteka-čija-se-veličina-prikazuje]
  local pid=$1 m=$2 f=${3-} i=0 t0=$SECONDS extra rc=0
  stop_spin; log "WAIT $m"
  if [[ -t 4 ]]; then tput civis >&4 2>/dev/null || true; else echo " … $m" >&4; fi
  while kill -0 "$pid" 2>/dev/null; do
    if [[ -t 4 ]]; then
      extra="$(( SECONDS - t0 ))s"
      [[ -n $f && -f $f ]] && extra="$(du -h "$f" 2>/dev/null | cut -f1) · $extra"
      printf '\r\e[K %s%s%s %s %s(%s)%s' "$C" "${FRAMES[i % 10]}" "$N" "$m" "$D" "$extra" "$N" >&4
      i=$((i + 1))
    fi
    sleep 0.3
  done
  wait "$pid" || rc=$?
  if [[ -t 4 ]]; then printf '\r\e[K' >&4; tput cnorm >&4 2>/dev/null || true; fi
  return "$rc"
}
info() { stop_spin; echo " ${C}➜${N} $*" >&4; log "INFO $*"; }
ok()   { stop_spin; echo " ${G}✔${N} $*" >&4; log "OK   $*"; }
warn() { stop_spin; echo " ${Y}⚠${N} ${Y}$*${N}" >&4; log "WARN $*"; }
show_log_tail() {
  [[ -s $LOG ]] || return 0
  echo " ${D}── zadnji retci loga ($LOG) ──${N}" >&4
  tail -n 12 "$LOG" | sed 's/^/   /' >&4
}
die()  { stop_spin; echo " ${R}✖ $*${N}" >&4; log "FAIL $*"; show_log_tail; exit 1; }
hdr()  { stop_spin; CUR_SECTION=$*; echo >&4; echo " ${B}${C}━━━ $* ━━━${N}" >&4; log "==== $*"; }
on_err() {
  local rc=$? ln=$1 cmd=$2
  stop_spin
  echo " ${R}✖ Greška u liniji $ln (kod $rc): $cmd${N}" >&4
  log "ERR line $ln rc $rc: $cmd"; show_log_tail
}
trap 'on_err $LINENO "$BASH_COMMAND"' ERR
on_exit() {
  local rc=$?
  stop_spin; if [[ -t 4 ]]; then tput cnorm >&4 2>/dev/null || true; fi
  if (( API_MODE )); then rm -f "$API_DIR/question.json" "$API_DIR/askpass"; echo "$rc" > "$API_DIR/exit"; fi
}
trap on_exit EXIT

banner() {
  (( API_MODE )) || clear >&4 2>/dev/null || true
  printf '%s' "$C" >&4
  cat >&4 <<'EOF'
       _      _ _        __ _
      | | ___| | |_   _ / _(_)_ __
   _  | |/ _ \ | | | | | |_| | '_ \
  | |_| |  __/ | | |_| |  _| | | | |
   \___/ \___|_|_|\__, |_| |_|_| |_|
                  |___/
EOF
  printf '%s   %sDocker ➜ Proxmox LXC%s  ·  NVENC / QSV  ·  NFS   %sv%s%s\n\n' "$N" "$B" "$N" "$D" "$SCRIPT_VERSION" "$N" >&4
}

# ----------------------------------------------------------------------------- whiptail
wt_w() { local c; c=$(tput cols 2>/dev/null || echo 80); c=$(( c - 10 )); (( c > 90 )) && c=90; (( c < 60 )) && c=60; echo "$c"; }
wt_rows() { tput lines 2>/dev/null || echo 24; }
wt_h() {   # visina prozora za tekst
  local t=$1 extra=${2:-7} n r
  n=$(printf '%s\n' "$t" | fold -s -w $(( $(wt_w) - 6 )) | wc -l)
  r=$(wt_rows); n=$(( n + extra )); (( n > r - 2 )) && n=$(( r - 2 )); echo "$n"
}
wt() { stop_spin; whiptail --backtitle "$WT_BT" --title " $CUR_SECTION " "$@" 3>&1 1>/dev/tty 2>&3 </dev/tty; }
wt_cancel() {
  if whiptail --backtitle "$WT_BT" --title " Prekid " --yes-button "Prekini" --no-button "Nastavi" \
       --yesno "Želiš li prekinuti?

Do završne potvrde na sustavu ništa nije promijenjeno." 11 60 3>&1 1>/dev/tty 2>&3 </dev/tty; then
    clear >&4 2>/dev/null || true; die "Prekinuto na zahtjev."
  fi
}
wt_msg() {   # wt_msg "naslov" "tekst"
  if (( API_MODE )); then echo " ━━ $1" >&4; printf '%s\n' "$2" | sed 's/^/    /' >&4; return 0; fi
  if (( USE_TUI )); then local s=$CUR_SECTION; CUR_SECTION=$1; wt --msgbox "$2" "$(wt_h "$2" 8)" "$(wt_w)" || true; CUR_SECTION=$s
  else echo; echo "${B}$1${N}"; echo "$2"; echo; fi
}
bad() { if (( API_MODE )); then API_LAST_ERR=$1; warn "$1"; elif (( USE_TUI )); then wt --msgbox "$1" "$(wt_h "$1" 7)" "$(wt_w)" || true; else warn "$1"; fi; }

# ----------------------------------------------------------------------------- pitanja preko JellyCleana (--api)
API_QSEQ=0; API_ANS=""; API_LAST_ERR=""
api_ask() {   # api_ask TIP "tekst" "zadano" [tag opis [ON|OFF]] ...   → API_ANS
  local type=$1 text=$2 def=${3-} opts='[]' a id
  shift 3 2>/dev/null || shift $#
  stop_spin
  API_QSEQ=$((API_QSEQ + 1)); id=$API_QSEQ
  if [[ $type == menu ]]; then
    opts=$(while (( $# >= 2 )); do printf '%s\t%s\n' "$1" "$2"; shift 2; done \
           | jq -Rs 'split("\n") | map(select(length>0) | split("\t") | {tag:.[0], label:.[1]})')
  elif [[ $type == checklist ]]; then
    opts=$(while (( $# >= 3 )); do printf '%s\t%s\t%s\n' "$1" "$2" "$3"; shift 3; done \
           | jq -Rs 'split("\n") | map(select(length>0) | split("\t") | {tag:.[0], label:.[1], on:(.[2]=="ON")})')
  fi
  ( umask 077
    jq -n --argjson id "$id" --arg type "$type" --arg section "$CUR_SECTION" --arg text "$text" \
          --arg def "$def" --arg err "$API_LAST_ERR" --argjson options "$opts" \
          '{id:$id, type:$type, section:$section, text:$text, default:$def, error:$err, options:$options}' \
      > "$API_DIR/question.tmp" )
  rm -f "$API_DIR/answer.json"
  mv -f "$API_DIR/question.tmp" "$API_DIR/question.json"
  API_LAST_ERR=""
  log "PITANJE #$id ($type): ${text%%$'\n'*}"
  local t0=$SECONDS
  while :; do
    if [[ -f $API_DIR/answer.json ]]; then
      a=$(cat "$API_DIR/answer.json"); rm -f "$API_DIR/answer.json"
      [[ $(jq -r '.id' <<<"$a" 2>/dev/null) == "$id" ]] && break
    fi
    if (( SECONDS - t0 > 7200 )); then rm -f "$API_DIR/question.json"; die "Iz JellyCleana 2 sata nije stigao odgovor – prekidam."; fi
    sleep 0.4
  done
  rm -f "$API_DIR/question.json"
  if [[ $(jq -r '.cancel // false' <<<"$a") == true ]]; then die "Prekinuto iz JellyCleana."; fi
  API_ANS=$(jq -r '.value // "" | if type == "array" then join("\n") else tostring end' <<<"$a")
  [[ $type == password ]] && log "ODGOVOR #$id: ***" || log "ODGOVOR #$id: ${API_ANS%%$'\n'*}"
}

# ----------------------------------------------------------------------------- pamćenje odgovora
# Svaki odgovor se odmah sprema u $DRAFT. Kod ponovnog pokretanja:
#   DRAFT_MODE=skip    → već odgovorena pitanja se preskaču
#   DRAFT_MODE=default → pitanja se postavljaju, prethodni odgovor je zadana vrijednost
DRAFT="$STATE_DIR/draft.env"
declare -A DRAFT_VAL=()
DRAFT_MODE=""
persistable() { [[ $1 =~ ^[A-Z][A-Z0-9_]*$ && $1 != SUDO_PW && $1 != DRAFT_* ]]; }
draft_write() {
  ( umask 077
    { declare -p DRAFT_VAL
      local a; for a in DRAFT_M_SRC DRAFT_M_DEST DRAFT_M_NFS DRAFT_M_LXC; do declare -p "$a" 2>/dev/null || true; done
    } | sed -E 's/^declare -([A-Za-z-]*)/declare -g\1/; s/^declare -g-/declare -g/' > "$DRAFT" )
}
draft_set()    { persistable "$1" || return 0; DRAFT_VAL[$1]=$2; draft_write; }
draft_forget() { unset "DRAFT_VAL[$1]"; draft_write; }
draft_skip()   { [[ $DRAFT_MODE == skip ]] && persistable "$1" && [[ -n ${DRAFT_VAL[$1]+x} ]]; }
draft_def()    { if [[ -n $DRAFT_MODE && -n ${DRAFT_VAL[$1]+x} ]]; then printf '%s' "${DRAFT_VAL[$1]}"; else printf '%s' "$2"; fi; }
draft_save_mounts() {
  declare -ga DRAFT_M_SRC=("${M_SRC[@]}") DRAFT_M_DEST=("${M_DEST[@]}") DRAFT_M_NFS=("${M_NFS[@]}") DRAFT_M_LXC=("${M_LXC[@]}")
  draft_write
}
draft_has_mounts() { [[ $DRAFT_MODE == skip && -n ${DRAFT_M_NFS+x} ]]; }

# ----------------------------------------------------------------------------- pitanja
ask() {   # ask VAR "pitanje" [zadano]
  local __var=$1 __q=$2 __def=${3-} __ans
  if draft_skip "$__var"; then printf -v "$__var" '%s' "${DRAFT_VAL[$__var]}"; log "DRAFT $__var"; return 0; fi
  __def=$(draft_def "$__var" "$__def")
  if (( API_MODE )); then api_ask input "$__q" "$__def"; __ans=$API_ANS
  elif (( USE_TUI )); then
    while :; do
      if __ans=$(wt --inputbox "$__q" "$(wt_h "$__q" 8)" "$(wt_w)" "$__def"); then break; fi
      wt_cancel
    done
  else
    if [[ -n $__def ]]; then read -rp "${B}?${N} $__q [${__def}]: " __ans </dev/tty
    else read -rp "${B}?${N} $__q: " __ans </dev/tty; fi
    __ans=${__ans:-$__def}
  fi
  printf -v "$__var" '%s' "$__ans"
  draft_set "$__var" "$__ans"
}
ask_req() { local __v=$1; while :; do ask "$@"; [[ -n ${!__v} ]] && break; draft_forget "$__v"; bad "Vrijednost je obavezna."; done; }
ask_num() { local __v=$1; while :; do ask "$@"; [[ ${!__v} =~ ^[0-9]+$ ]] && break; draft_forget "$__v"; bad "Upiši cijeli broj."; done; }
ask_yn() {  # ask_yn "pitanje" [y|n] [KLJUČ-za-pamćenje]  -> 0 = da
  local q=$1 d=${2:-y} key=${3:-} a rc res=""
  if [[ -n $key ]]; then
    if draft_skip "$key"; then log "DRAFT $key"; if [[ ${DRAFT_VAL[$key]} == y ]]; then return 0; fi; return 1; fi
    d=$(draft_def "$key" "$d")
  fi
  if (( API_MODE )); then
    api_ask yesno "$q" "$d"
    if [[ ${API_ANS,,} == y || ${API_ANS,,} == yes || ${API_ANS,,} == da || $API_ANS == true ]]; then res=y; else res=n; fi
  elif (( USE_TUI )); then
    local opt=(); [[ $d == n ]] && opt=(--defaultno)
    while [[ -z $res ]]; do
      wt "${opt[@]}" --yes-button "Da" --no-button "Ne" --yesno "$q" "$(wt_h "$q")" "$(wt_w)" && rc=0 || rc=$?
      case $rc in 0) res=y ;; 1) res=n ;; *) wt_cancel ;; esac
    done
  else
    local hint; [[ $d == y ]] && hint="D/n" || hint="d/N"
    while [[ -z $res ]]; do
      read -rp "${B}?${N} $q [$hint]: " a </dev/tty; a=${a:-$d}
      case ${a,,} in d|da|y|yes) res=y ;; n|ne|no) res=n ;; esac
    done
  fi
  if [[ -n $key ]]; then draft_set "$key" "$res"; fi
  if [[ $res == y ]]; then return 0; fi
  return 1
}
ask_pw() {   # jedna lozinka (bez ponavljanja)
  local __pv=$1 __pq=$2 __pw=""
  if (( API_MODE )); then api_ask password "$__pq" ""; __pw=$API_ANS
  elif (( USE_TUI )); then
    while :; do if __pw=$(wt --passwordbox "$__pq" "$(wt_h "$__pq" 8)" "$(wt_w)"); then break; fi; wt_cancel; done
  else read -rsp "${B}?${N} $__pq: " __pw </dev/tty; echo >&4; fi
  printf -v "$__pv" '%s' "$__pw"
}
ask_secret() {
  local __sv=$1 __sq=$2 __s1="" __s2=""
  if draft_skip "$__sv" && [[ -n ${DRAFT_VAL[$__sv]} ]]; then printf -v "$__sv" '%s' "${DRAFT_VAL[$__sv]}"; log "DRAFT $__sv"; return 0; fi
  while :; do
    ask_pw __s1 "$__sq"; ask_pw __s2 "Ponovi lozinku:"
    if [[ -n $__s1 && $__s1 == "$__s2" ]]; then printf -v "$__sv" '%s' "$__s1"; draft_set "$__sv" "$__s1"; return; fi
    bad "Lozinke nisu iste ili su prazne."
  done
}
ask_menu() {   # ask_menu VAR "pitanje" ZADANO tag opis [tag opis ...]
  local __var=$1 __q=$2 __def=$3; shift 3
  local __n=$(( $# / 2 )) __ans="" __k
  if draft_skip "$__var"; then   # zapamćeni odabir vrijedi samo ako je još ponuđen
    for (( __k = 1; __k <= $#; __k += 2 )); do
      if [[ ${!__k} == "${DRAFT_VAL[$__var]}" ]]; then printf -v "$__var" '%s' "${DRAFT_VAL[$__var]}"; log "DRAFT $__var"; return 0; fi
    done
  fi
  __def=$(draft_def "$__var" "$__def")
  if (( API_MODE )); then
    api_ask menu "$__q" "$__def" "$@"; __ans=$API_ANS
    local __ok=0
    for (( __k = 1; __k <= $#; __k += 2 )); do [[ ${!__k} == "$__ans" ]] && __ok=1; done
    (( __ok )) || __ans=${__def:-$1}
  elif (( USE_TUI )); then
    local __r __lh __h __opt=()
    __r=$(wt_rows); __lh=$__n; (( __lh > __r - 12 )) && __lh=$(( __r - 12 )); (( __lh < 1 )) && __lh=1
    __h=$(( $(wt_h "$__q" 7) + __lh )); (( __h > __r - 2 )) && __h=$(( __r - 2 ))
    [[ -n $__def ]] && __opt=(--default-item "$__def")
    while :; do
      if __ans=$(wt "${__opt[@]}" --ok-button "Odaberi" --cancel-button "Prekid" --menu "$__q" "$__h" "$(wt_w)" "$__lh" "$@"); then break; fi
      wt_cancel
    done
  else
    echo "$__q" >&4
    local __i=0 __tags=() __t
    while (( $# )); do __i=$((__i + 1)); __tags+=("$1"); printf '   %2d) %-16s %s\n' "$__i" "$1" "$2" >&4; shift 2; done
    while :; do
      read -rp "${B}?${N} Odabir (broj ili oznaka) [${__def}]: " __t </dev/tty; __t=${__t:-$__def}
      if [[ $__t =~ ^[0-9]+$ ]] && (( __t >= 1 && __t <= __i )); then __ans=${__tags[$((__t - 1))]}; break; fi
      for __ans in "${__tags[@]}"; do [[ $__ans == "$__t" ]] && break 2; done
      warn "Neispravan odabir."
    done
  fi
  printf -v "$__var" '%s' "$__ans"
  draft_set "$__var" "$__ans"
}
CHECK_RESULT=()
ask_check() {   # ask_check "pitanje" tag opis ON|OFF ...  → CHECK_RESULT
  local q=$1; shift
  local n=$(( $# / 3 )) out
  CHECK_RESULT=()
  if (( API_MODE )); then
    api_ask checklist "$q" "" "$@"
    [[ -n $API_ANS ]] && mapfile -t CHECK_RESULT <<<"$API_ANS"
  elif (( USE_TUI )); then
    local r lh h
    r=$(wt_rows); lh=$n; (( lh > r - 12 )) && lh=$(( r - 12 )); (( lh < 1 )) && lh=1
    h=$(( $(wt_h "$q" 7) + lh )); (( h > r - 2 )) && h=$(( r - 2 ))
    while :; do
      if out=$(wt --separate-output --ok-button "Potvrdi" --cancel-button "Prekid" --checklist "$q" "$h" "$(wt_w)" "$lh" "$@"); then break; fi
      wt_cancel
    done
    [[ -n $out ]] && mapfile -t CHECK_RESULT <<<"$out"
  else
    echo "$q" >&4
    while (( $# )); do
      if ask_yn "  $2" "$([[ $3 == ON ]] && echo y || echo n)"; then CHECK_RESULT+=("$1"); fi
      shift 3
    done
  fi
  return 0
}

# ============================================================================= stanje
SAVE_VARS=(DOCKER_HOST_IP SSH_USER SSH_PORT SUDO_MODE CONTAINER CONFIG_SRC CACHE_INNER SRC_VERSION
  M_SRC M_DEST M_NFS M_LXC MEDIA_RO MEDIA_UID MEDIA_GID
  CT_ID CT_HOSTNAME CT_TPL_STORE CT_STORAGE CT_DISK CT_CORES CT_RAM CT_SWAP CT_BRIDGE CT_VLAN
  CT_IP CT_GW CT_DNS CT_UNPRIV CT_PASSWORD CT_SSHKEYS CT_ONBOOT
  JF_VERSION_MODE APPLY_HWACCEL DISABLE_OLD NV_VERSION
  GPU_MODE INTEL_RENDER NEW_SERVER_ID JF_SERVER_NAME)

save_answers() {
  ( umask 077
    declare -p "${SAVE_VARS[@]}" 2>/dev/null | sed -E 's/^declare (-[^ ]*)/declare -g \1/' > "$ANSWERS" )
}
run_phase() {
  local name=$1; shift
  if [[ -f $STATE_DIR/.done_$name ]]; then ok "Korak '$name' je već odrađen – preskačem."; return; fi
  "$@"
  touch "$STATE_DIR/.done_$name"
  save_answers
}

# ============================================================================= SSH prema Docker hostu
ssh_opts() {
  SSH_OPTS=(-o ControlMaster=auto -o ControlPath="$SSH_CTL" -o ControlPersist=60m
            -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o LogLevel=ERROR -p "$SSH_PORT")
}
ssh_base() { ssh_opts; ssh "${SSH_OPTS[@]}" "$SSH_USER@$DOCKER_HOST_IP" "$@"; }
_rcmd() { local b; b=$(printf '%s' "$1" | base64 -w0); printf 'bash -c "$(echo %s | base64 -d)"' "$b"; }
rsh()   { ssh_base "$(_rcmd "$1")" </dev/null; }
rsudo() {
  local c; c=$(_rcmd "$1")
  case $SUDO_MODE in
    none)   ssh_base "$c" </dev/null ;;
    nopass) ssh_base "sudo -n $c" </dev/null ;;
    pass)   ssh_base "sudo -S -p '' $c" <<<"$SUDO_PW" ;;
  esac
}
ssh_connect() {
  mkdir -p /root/.ssh && chmod 700 /root/.ssh
  ssh_opts
  if ssh "${SSH_OPTS[@]}" -O check "$SSH_USER@$DOCKER_HOST_IP" 2>/dev/null </dev/null; then return 0; fi
  if ssh "${SSH_OPTS[@]}" -o BatchMode=yes "$SSH_USER@$DOCKER_HOST_IP" true 2>/dev/null </dev/null; then
    ok "SSH veza na $SSH_USER@$DOCKER_HOST_IP (ključ)."; return 0
  fi
  if (( API_MODE )); then   # lozinka iz JellyCleana → ssh preko SSH_ASKPASS (bez terminala, nigdje se ne sprema)
    local __sp="" __i
    printf '#!/bin/sh\nprintf "%%s\\n" "$JC_SSHPASS"\n' > "$API_DIR/askpass"; chmod 700 "$API_DIR/askpass"
    for __i in 1 2 3; do
      ask_pw __sp "SSH lozinka za $SSH_USER@$DOCKER_HOST_IP:"
      msg_info "Spajam se na $SSH_USER@$DOCKER_HOST_IP:$SSH_PORT…"
      if JC_SSHPASS=$__sp SSH_ASKPASS="$API_DIR/askpass" SSH_ASKPASS_REQUIRE=force DISPLAY=:0 setsid -w \
           ssh "${SSH_OPTS[@]}" -o NumberOfPasswordPrompts=1 -o PreferredAuthentications=password,keyboard-interactive \
           "$SSH_USER@$DOCKER_HOST_IP" true </dev/null >>"$LOG" 2>&1; then
        rm -f "$API_DIR/askpass"; ok "SSH veza uspostavljena."; return 0
      fi
      bad "SSH prijava na $SSH_USER@$DOCKER_HOST_IP nije uspjela – provjeri lozinku."
    done
    rm -f "$API_DIR/askpass"; die "SSH veza prema $DOCKER_HOST_IP nije uspjela."
  fi
  (( USE_TUI )) && clear >&4 2>/dev/null
  echo >&4
  info "Spajam se na ${B}$SSH_USER@$DOCKER_HOST_IP:$SSH_PORT${N} – upiši SSH lozinku:"
  ssh_base true </dev/tty || die "SSH veza prema $DOCKER_HOST_IP nije uspjela."
  ok "SSH veza uspostavljena."
}
detect_sudo() {
  if [[ $SSH_USER == root ]]; then SUDO_MODE=none
  elif rsh 'sudo -n true' 2>/dev/null; then SUDO_MODE=nopass
  else SUDO_MODE=pass; fi
  ensure_sudo_pw
  rsudo 'command -v docker >/dev/null' || die "Na $DOCKER_HOST_IP nije pronađen 'docker'."
}
ensure_sudo_pw() {
  [[ ${SUDO_MODE:-} == pass && -z ${SUDO_PW:-} ]] || return 0
  local i
  for i in 1 2 3; do
    ask_pw SUDO_PW "Korisnik '$SSH_USER' na $DOCKER_HOST_IP treba sudo za Docker i čitanje Jellyfin podataka.

sudo lozinka:"
    msg_info "Provjeravam sudo lozinku…"
    if rsudo 'true' >/dev/null 2>&1; then ok "sudo radi."; return 0; fi
    SUDO_PW=""; bad "sudo lozinka nije ispravna."
  done
  die "sudo lozinka nije ispravna."
}

# ============================================================================= pomoćne
preflight() {
  [[ $EUID -eq 0 ]] || die "Pokreni kao root na Proxmox hostu."
  command -v pct >/dev/null && command -v pveversion >/dev/null || die "Ovo nije Proxmox VE host."
  local missing=()
  command -v jq       >/dev/null || missing+=(jq)
  command -v curl     >/dev/null || missing+=(curl)
  command -v ssh      >/dev/null || missing+=(openssh-client)
  command -v lspci    >/dev/null || missing+=(pciutils)
  command -v mount.nfs >/dev/null || missing+=(nfs-common)
  (( API_MODE )) || command -v whiptail >/dev/null || missing+=(whiptail)
  if ((${#missing[@]})); then
    msg_info "Instaliram potrebne alate na hostu: ${missing[*]}"
    run apt-get update -qq && run apt-get install -y -qq "${missing[@]}"
    ok "Alati instalirani: ${missing[*]}"
    if [[ " ${missing[*]} " == *" whiptail "* && " $* " != *" --text "* && -t 4 ]]; then USE_TUI=1; fi
  fi
}

guest_exists() {
  pvesh get /cluster/resources --type vm --output-format json 2>/dev/null \
    | jq -e --arg id "$1" '.[] | select(.vmid == ($id|tonumber))' >/dev/null
}

NFS_LAST_OWNER=""; NFS_LAST_COUNT=0
nfs_test() {   # testno montira NFS s hosta; puni NFS_LAST_OWNER ("uid gid") i NFS_LAST_COUNT
  local src=$1 t
  t=$(mktemp -d)
  if timeout 25 mount -t nfs -o ro,soft,timeo=50,retrans=1 "$src" "$t" >>"$LOG" 2>&1; then
    NFS_LAST_COUNT=$(ls -1 "$t" 2>/dev/null | wc -l)
    NFS_LAST_OWNER=$(find "$t" -mindepth 1 -maxdepth 1 -printf '%U %G\n' 2>/dev/null | sort | uniq -c | sort -rn | awk 'NR==1{print $2" "$3}')
    [[ -n $NFS_LAST_OWNER ]] || NFS_LAST_OWNER=$(stat -c '%u %g' "$t")
    umount "$t" 2>/dev/null || umount -l "$t"; rmdir "$t"
    return 0
  fi
  rmdir "$t"; return 1
}

host_driver_version() {   # radi i kad je drajver iz Debian paketa bez nvidia-smi
  local v=""
  v=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n1 || true)
  [[ -z $v && -r /sys/module/nvidia/version ]] && v=$(cat /sys/module/nvidia/version)
  echo "$v"
}

latest_driver_for_gpu() {   # Maxwell/Pascal → zadnja 580.x, novije → najnovija
  if (( NV_LEGACY )); then
    curl -fsSL -m 20 https://download.nvidia.com/XFree86/Linux-x86_64/ 2>/dev/null \
      | grep -oE "${LEGACY_BRANCH}\.[0-9]+(\.[0-9]+)?/" | tr -d / | sort -uV | tail -n1 || true
  else
    curl -fsSL -m 20 https://download.nvidia.com/XFree86/Linux-x86_64/latest.txt 2>/dev/null | awk '{print $1}' || true
  fi
}

NV_RUN=""
dl_nvidia_run() {   # puni NV_RUN
  local v=$1 url="https://download.nvidia.com/XFree86/Linux-x86_64/$1/NVIDIA-Linux-x86_64-$1.run"
  NV_RUN="$STATE_DIR/NVIDIA-Linux-x86_64-$v.run"
  [[ -s $NV_RUN ]] && return 0
  curl -fsI -m 20 "$url" >/dev/null || die "NVIDIA drajver $v ne postoji na $url"
  ( trap - ERR; set +e; curl -fsSL -o "$NV_RUN.part" "$url" ) &
  spin_wait $! "Preuzimam NVIDIA $v" "$NV_RUN.part" || die "Preuzimanje NVIDIA drajvera nije uspjelo."
  mv "$NV_RUN.part" "$NV_RUN"
  ok "NVIDIA $v preuzet."
}

# --- otkrivanje GPU-ova
nv_detect() {
  NV_ADDR=$(lspci -D -nn -d 10de: | grep -Ei 'VGA|3D controller' | awk 'NR==1{print $1}' || true)
  NV_NAME=""; NV_LEGACY=0
  if [[ -n $NV_ADDR ]]; then
    NV_NAME=$(lspci -s "$NV_ADDR" | cut -d: -f3- | sed 's/^ *//')
    if [[ $NV_NAME =~ GP10[0-9]|GM[12]0[0-9]|GK1[01][0-9] ]]; then NV_LEGACY=1; fi
  fi
  return 0
}
intel_detect() {
  local r
  INTEL_RENDER_DET=""; INTEL_NAME=""
  for r in /sys/class/drm/renderD*; do
    [[ -e $r/device/vendor ]] || continue
    if [[ $(cat "$r/device/vendor") == 0x8086 ]]; then
      INTEL_RENDER_DET="/dev/dri/${r##*/}"
      INTEL_NAME=$(lspci -s "$(basename "$(readlink -f "$r/device")")" | cut -d: -f3- | sed 's/^ *//')
      break
    fi
  done
  return 0
}
gpu_codecs() {   # HW dekoderi ovisno o generaciji GPU-a
  local base="h264 hevc mpeg2video vc1 vp8 vp9"
  if [[ $GPU_MODE == nvidia ]]; then
    nv_detect
    if [[ $NV_NAME =~ GA10[0-9]|AD10[0-9]|GB20[0-9]|RTX\ [345]0[0-9]{2} ]]; then base+=" av1"; fi
  else
    intel_detect
    if [[ $INTEL_NAME =~ Iris\ Xe|Arc|UHD\ Graphics\ 7[0-9]{2}|Alder|Raptor|Meteor|Lunar|Arrow ]]; then base+=" av1"; fi
  fi
  echo "$base"
}

# --- storage: tip i fizički disk iza njega
storage_dev() {
  local s=$1 t vg path dev=""
  t=$(pvesm status 2>/dev/null | awk -v s="$s" '$1==s{print $2}')
  case $t in
    lvmthin|lvm)
      vg=$(awk -v s="$s" '/^[^ \t]/{f=($1 ~ /^lvm(thin)?:$/ && $2==s)} f && $1=="vgname"{print $2; exit}' /etc/pve/storage.cfg)
      [[ -n $vg ]] && dev=$(pvs --noheadings -o pv_name -S "vg_name=$vg" 2>/dev/null | xargs || true) ;;
    zfspool)
      path=$(awk -v s="$s" '/^[^ \t]/{f=($1=="zfspool:" && $2==s)} f && $1=="pool"{print $2; exit}' /etc/pve/storage.cfg)
      [[ -n $path ]] && dev=$(zpool status -P "${path%%/*}" 2>/dev/null | grep -oE '/dev/[^ ]+' | xargs || true) ;;
    dir)
      path=$(awk -v s="$s" '/^[^ \t]/{f=($1=="dir:" && $2==s)} f && $1=="path"{print $2; exit}' /etc/pve/storage.cfg)
      [[ -n $path ]] && dev=$(df --output=source "$path" 2>/dev/null | tail -n1 || true) ;;
  esac
  echo "${t:-?}|${dev:-?}"
}

# --- provjera putanja biblioteka prema NFS-u (prije kreiranja LXC-a)
#     Jellyfin u bazi pamti pune putanje (npr. /nas/Gondolin/media/series), pa
#     mount u LXC-u mora biti točno tamo gdje je bio u Dockeru.
validate_library_paths() {
  local libs=() tdirs=() p i t rel tail pref k n parts changed pass sugg_i sugg_p missing=()
  mapfile -t libs < <(rsudo "find $(printf %q "$CONFIG_SRC") -path '*/root/default/*/options.xml' -exec cat {} + 2>/dev/null" 2>/dev/null \
                      | grep -oP '<Path>\K[^<]+' | sort -u || true)
  if ((${#libs[@]} == 0)) || ((${#M_NFS[@]} == 0)); then return 0; fi
  log "Putanje biblioteka: ${libs[*]}"

  msg_info "Provjeravam putanje biblioteka na NAS-u…"
  for i in "${!M_NFS[@]}"; do
    t=$(mktemp -d); tdirs+=("$t")
    timeout 25 mount -t nfs -o ro,soft,timeo=50,retrans=1 "${M_NFS[$i]}" "$t" >>"$LOG" 2>&1 || true
  done

  for pass in 1 2 3; do
    changed=0; missing=()
    for p in "${libs[@]}"; do
      # 1) postoji li pod nekim mountom na očekivanoj putanji?
      local found=0
      for i in "${!M_NFS[@]}"; do
        if [[ $p == "${M_LXC[$i]}" || $p == "${M_LXC[$i]%/}"/* ]]; then
          rel=${p#"${M_LXC[$i]%/}"}
          if [[ -d ${tdirs[$i]}$rel ]]; then found=1; break; fi
        fi
      done
      (( found )) && continue
      # 2) potraži isti kraj putanje unutar NFS sharea → izračunaj ispravnu točku montiranja
      sugg_i=""; sugg_p=""
      IFS=/ read -ra parts <<<"${p#/}"; n=${#parts[@]}
      for i in "${!M_NFS[@]}"; do
        mountpoint -q "${tdirs[$i]}" || continue
        for (( k = 1; k < n; k++ )); do
          tail="/$(IFS=/; echo "${parts[*]:k}")"
          if [[ -d ${tdirs[$i]}$tail ]]; then pref="/$(IFS=/; echo "${parts[*]:0:k}")"; sugg_i=$i; sugg_p=$pref; break 2; fi
        done
      done
      if [[ -n $sugg_i && $sugg_p != "${M_LXC[$sugg_i]}" ]]; then
        stop_spin
        if ask_yn "Biblioteka u Jellyfin bazi:
   $p

Nalazi se na NAS-u ${M_NFS[$sugg_i]},
ali s mountom na ${M_LXC[$sugg_i]} u LXC-u ne bi bila na toj putanji.

Ispravna točka montiranja je:  $sugg_p

Promijeniti mount  ${M_LXC[$sugg_i]}  ➜  $sugg_p ?" y; then
          ok "Mount ${M_NFS[$sugg_i]}: ${M_LXC[$sugg_i]} ➜ $sugg_p"
          M_LXC[$sugg_i]=$sugg_p; changed=1; break
        fi
      fi
      missing+=("$p")
    done
    (( changed )) || break
  done

  for t in "${tdirs[@]}"; do umount "$t" >/dev/null 2>&1 || umount -l "$t" >/dev/null 2>&1 || true; rmdir "$t" 2>/dev/null || true; done
  stop_spin
  if ((${#missing[@]})); then
    local list; list=$(printf '   %s\n' "${missing[@]}")
    warn "Biblioteke koje nisu pronađene na NAS-u: ${missing[*]}"
    ask_yn "Ove putanje biblioteka NE postoje ni pod jednim NFS mountom:

$list

Jellyfin ih neće vidjeti – NEMOJ skenirati biblioteke dok to ne središ,
inače se stavke mogu obrisati iz baze zajedno s poviješću gledanja.

Svejedno nastaviti?" n || die "Prekinuto – dodaj ili ispravi NFS mount za te biblioteke."
  else
    ok "Sve putanje biblioteka (${#libs[@]}) postoje na NAS-u na istim mjestima kao u Dockeru."
  fi
}

# ============================================================================= ČAROBNJAK
collect_answers() {
  banner
  wt_msg "Dobrodošli" "Ovaj čarobnjak premješta Jellyfin iz Docker kontejnera u novi LXC na ovom Proxmox hostu, sa samostalnim Jellyfinom.

   1  Provjera grafike na ovom hostu (NVIDIA ili Intel)
   2  Kratko zaustavljanje Docker Jellyfina i izvoz postavki i baze
   3  Novi Debian LXC s GPU-om i NFS medijima s NAS-a
   4  Instalacija Jellyfina, vraćanje podataka, HW transkodiranje

Stari Docker kontejner se ne briše. Na sustavu se ništa ne mijenja prije završne potvrde.$( (( API_MODE )) || printf '\n\nTipke:  ↑↓ odabir · RAZMAKNICA označi · TAB gumbi · ENTER potvrdi · ESC prekid')"

  # ---------------------------------------------------------------- izvor
  CUR_SECTION="1/4 · Izvor (Docker Jellyfin)"
  ask_req DOCKER_HOST_IP "IP adresa ili hostname VM-a na kojem radi Docker Jellyfin.

Može biti i na drugom (starom) Proxmox serveru – podaci se povlače preko SSH-a." ""
  ask_req SSH_USER "SSH korisnik na $DOCKER_HOST_IP:" root
  ask_num SSH_PORT "SSH port:" 22
  ssh_connect
  detect_sudo

  local clist=() items=() l n i s defc=""
  msg_info "Dohvaćam popis Docker kontejnera…"
  mapfile -t clist < <(rsudo "docker ps -a --format '{{.Names}}|{{.Image}}|{{.State}}'")
  stop_spin
  ((${#clist[@]})) || die "Na Docker hostu nema kontejnera."
  for l in "${clist[@]}"; do
    IFS='|' read -r n i s <<<"$l"
    if [[ -z $defc && ${i,,} == *jellyfin* ]]; then defc=$n; fi
    items+=("$n" "$(printf '%-9s %s' "$s" "$i")")
  done
  ask_menu CONTAINER "Odaberi Jellyfin kontejner na $DOCKER_HOST_IP:" "$defc" "${items[@]}"

  local J="$STATE_DIR/docker-inspect.json"
  rsudo "docker inspect $(printf %q "$CONTAINER")" > "$J" 2>>"$LOG" || die "Kontejner '$CONTAINER' ne postoji."
  ok "Kontejner: $CONTAINER  ($(jq -r '.[0].Config.Image' "$J"))"

  CONFIG_SRC=$(jq -r '[.[0].Mounts[] | select(.Destination=="/config")][0].Source // empty' "$J")
  [[ -n $CONFIG_SRC ]] || ask_req CONFIG_SRC "Kontejner nema mount na /config.

Upiši putanju Jellyfin config/data direktorija na Docker hostu:"
  rsudo "test -d $(printf %q "$CONFIG_SRC")" || die "Direktorij $CONFIG_SRC ne postoji na Docker hostu."
  ok "Jellyfin podaci: $CONFIG_SRC"
  if jq -e '.[0].Mounts[] | select(.Destination=="/cache")' "$J" >/dev/null 2>&1; then CACHE_INNER=/cache
  else CACHE_INNER=/config/cache; fi

  # ---------------------------------------------------------------- mediji / NFS
  CUR_SECTION="1/4 · Mediji (NFS s NAS-a)"
  local mlines=() mt msrc mdest fi fs_src fs_type fs_tgt det nfs lxcp sub hint chk=() k
  mapfile -t mlines < <(jq -r '.[0].Mounts[]
      | select((.Destination | IN("/config","/cache","/etc/localtime","/etc/timezone")) | not)
      | select(.Destination | test("^/(dev|proc|sys|run|tmp|var/run)(/|$)|transcod") | not)
      | "\(.Type)|\(.Source)|\(.Destination)"' "$J")
  M_SRC=(); M_DEST=(); M_NFS=(); M_LXC=()
  # zapamćeni mountovi iz prošlog pokretanja (po Docker odredištu)
  local -A prev_nfs=() prev_lxc=(); local pj on
  if [[ -n $DRAFT_MODE && -n ${DRAFT_M_NFS+x} ]]; then
    for pj in "${!DRAFT_M_NFS[@]}"; do prev_nfs[${DRAFT_M_DEST[$pj]}]=${DRAFT_M_NFS[$pj]}; prev_lxc[${DRAFT_M_DEST[$pj]}]=${DRAFT_M_LXC[$pj]}; done
  fi
  if draft_has_mounts; then
    M_SRC=("${DRAFT_M_SRC[@]}"); M_DEST=("${DRAFT_M_DEST[@]}"); M_NFS=("${DRAFT_M_NFS[@]}"); M_LXC=("${DRAFT_M_LXC[@]}")
    for pj in "${!M_NFS[@]}"; do ok "Zapamćeni mount: ${M_NFS[$pj]} ➜ ${M_LXC[$pj]}"; done
  elif ((${#mlines[@]})); then
    k=0
    for l in "${mlines[@]}"; do
      IFS='|' read -r mt msrc mdest <<<"$l"; k=$((k + 1)); on=ON
      if [[ -n $DRAFT_MODE && -n ${DRAFT_M_NFS+x} && -z ${prev_nfs[$mdest]+x} ]]; then on=OFF; fi
      chk+=("$k" "$msrc  ➜  $mdest" "$on")
    done
    ask_check "Mountovi kontejnera '$CONTAINER'.

Označi one s medijima koje novi Jellyfin treba (RAZMAKNICA = označi/odznači):" "${chk[@]}"
    for k in "${CHECK_RESULT[@]}"; do
      IFS='|' read -r mt msrc mdest <<<"${mlines[$((k - 1))]}"
      det=""
      fi=$(rsh "findmnt -n -o SOURCE,FSTYPE,TARGET -T $(printf %q "$msrc") 2>/dev/null" || true)
      read -r fs_src fs_type fs_tgt <<<"$fi"
      if [[ $fs_type == nfs* ]]; then
        if [[ $fs_src =~ ^(.*)\[(.*)\]$ ]]; then fs_src="${BASH_REMATCH[1]%/}${BASH_REMATCH[2]}"; fi
        sub=${msrc#"$fs_tgt"}; det="${fs_src%/}${sub}"
        hint="Na Docker hostu je ovo NFS mount – izvor je prepoznat automatski."
      elif [[ $fs_type == cifs || $fs_type == smb3 ]]; then
        hint="Na Docker hostu je ovo SMB share:  $fs_src${msrc#"$fs_tgt"}
Upiši NFS export na NAS-u koji sadrži iste mape (npr. 192.168.1.10:/volume1/media/movies)."
      else
        hint="Na Docker hostu ovo nije mrežni disk (${fs_type:-nepoznato}). Upiši NFS export na NAS-u s istim podacima."
      fi
      ask_req nfs "Docker mount:  $msrc  ➜  $mdest

$hint

NFS izvor (NAS:/putanja):" "${prev_nfs[$mdest]:-$det}"
      ask_req lxcp "Putanja u novom LXC-u za  $nfs

Preporuka: ISTA kao u Dockeru ($mdest) – tada biblioteke, gledanost i metapodaci ostaju ispravni bez ponovnog skeniranja." "${prev_lxc[$mdest]:-$mdest}"
      if [[ $lxcp != "$mdest" ]]; then bad "Putanja se razlikuje od Dockera ($mdest).

Biblioteke ćeš morati ručno prepraviti u Jellyfinu i ponovno ih skenirati."; fi
      M_SRC+=("$msrc"); M_DEST+=("$mdest"); M_NFS+=("$nfs"); M_LXC+=("$lxcp")
    done
  fi
  if ! draft_has_mounts; then
    while ask_yn "Dodati još jedan NFS mount ručno?" n; do
      ask_req nfs  "NFS izvor (NAS:/export/putanja):"
      ask_req lxcp "Putanja u LXC-u za $nfs:"
      M_SRC+=(""); M_DEST+=("$lxcp"); M_NFS+=("$nfs"); M_LXC+=("$lxcp")
    done
  fi
  ((${#M_NFS[@]})) || bad "Nisi odabrao nijedan medijski mount – Jellyfin neće vidjeti medije dok ih ne dodaš ručno."
  draft_save_mounts

  local idx nfs_owner=""
  for idx in "${!M_NFS[@]}"; do
    msg_info "Provjeravam NFS ${M_NFS[$idx]}…"
    if nfs_test "${M_NFS[$idx]}"; then
      ok "NFS ${M_NFS[$idx]} dostupan ($NFS_LAST_COUNT stavki, vlasnik UID/GID $NFS_LAST_OWNER)"
      [[ -z $nfs_owner ]] && nfs_owner=$NFS_LAST_OWNER
    else
      stop_spin
      ask_yn "Ne mogu montirati  ${M_NFS[$idx]}  s ovog Proxmox hosta.

Provjeri:
 • je li putanja točna (showmount -e <IP-NAS-a>)
 • dopušta li NFS export IP ovog Proxmoxa (i LXC-a)
 • je li NFS servis uključen na NAS-u

Svejedno nastaviti?" n || die "Prekinuto – ispravi NFS pa pokreni ponovno."
    fi
  done
  validate_library_paths
  draft_save_mounts
  if ask_yn "Montirati medije SAMO ZA ČITANJE?

(Jellyfin tada ne može spremati NFO datoteke i slike uz medije.)" n YN_MEDIA_RO; then MEDIA_RO=1; else MEDIA_RO=0; fi

  # UID/GID – NFS prikazuje stvarne vlasnike na NAS-u, pa je to najtočniji izvor
  local u="" g="" ug src_txt=""
  if [[ -n $nfs_owner ]]; then read -r u g <<<"$nfs_owner"; src_txt="pročitano s NFS-a"; fi
  if [[ -z $u ]]; then
    ug=$(jq -r '.[0].Config.User // empty' "$J")
    if [[ $ug =~ ^([0-9]+)(:([0-9]+))?$ ]]; then u=${BASH_REMATCH[1]}; g=${BASH_REMATCH[3]:-}; src_txt="iz Docker postavki"; fi
  fi
  if [[ -z $u ]]; then
    u=$(jq -r '.[0].Config.Env[]? | select(startswith("PUID=")) | sub("PUID=";"")' "$J" | head -n1)
    g=$(jq -r '.[0].Config.Env[]? | select(startswith("PGID=")) | sub("PGID=";"")' "$J" | head -n1)
    [[ -n $u ]] && src_txt="PUID/PGID iz Dockera"
  fi
  ask_num MEDIA_UID "Jellyfin u LXC-u radit će s UID/GID-om vlasnika medija na NAS-u, da NFS prava rade.

Prijedlog: ${u:-1000}/${g:-${u:-1000}}${src_txt:+  ($src_txt)}

UID vlasnika medija (0 = ne mijenjaj):" "${u:-1000}"
  ask_num MEDIA_GID "GID vlasnika medija (0 = ne mijenjaj):" "${g:-${u:-1000}}"

  # ---------------------------------------------------------------- verzija i veličina
  CUR_SECTION="1/4 · Izvor (Docker Jellyfin)"
  msg_info "Određujem verziju Jellyfina i veličinu podataka…"
  local port bytes gb sugg
  port=$(jq -r '.[0].NetworkSettings.Ports["8096/tcp"][0].HostPort // empty' "$J" 2>/dev/null || true)
  SRC_VERSION=$(curl -fsS -m 5 "http://$DOCKER_HOST_IP:${port:-8096}/System/Info/Public" 2>/dev/null | jq -r '.Version // empty' 2>/dev/null || true)
  if [[ -z $SRC_VERSION ]]; then
    SRC_VERSION=$(rsudo "C=$(printf %q "$CONFIG_SRC"); "'for d in "$C/log" "$C/data/log"; do [ -d "$d" ] && ls -t "$d"/*.log 2>/dev/null | head -n 3; done | xargs -r grep -hoE "Jellyfin version: [0-9]+\.[0-9]+\.[0-9]+" 2>/dev/null | head -n1 | awk "{print \$3}"' 2>/dev/null || true)
  fi
  if [[ -z $SRC_VERSION ]]; then
    SRC_VERSION=$(jq -r '.[0].Config.Labels["org.opencontainers.image.version"] // empty' "$J" | grep -oE '^[0-9]+\.[0-9]+\.[0-9]+' || true)
  fi
  bytes=$(rsudo "du -sb --exclude=cache --exclude=transcodes --exclude=log $(printf %q "$CONFIG_SRC") 2>/dev/null | cut -f1" 2>/dev/null || echo 0)
  gb=$(( (${bytes:-0} + 1073741823) / 1073741824 )); sugg=$(( gb * 2 + 8 )); (( sugg < 16 )) && sugg=16
  ok "Jellyfin podaci: ~${gb} GB (bez cachea i logova)"
  ask_req SRC_VERSION "Verzija Jellyfina u Dockeru:" "$SRC_VERSION"
  if dpkg --compare-versions "$SRC_VERSION" lt 10.10.0 2>/dev/null; then
    ask_yn "Docker verzija ($SRC_VERSION) je starija od 10.10.

Preporuka je prvo nadograditi Docker Jellyfin na 10.10.x pa onda migrirati.

Svejedno nastaviti?" n YN_OLD_VERSION || die "Prekinuto."
  fi

  # ---------------------------------------------------------------- LXC
  CUR_SECTION="2/4 · Novi LXC"
  local nextid; nextid=$(pvesh get /cluster/nextid 2>/dev/null || echo 200)
  while :; do
    ask_num CT_ID "ID novog LXC-a:" "$nextid"
    if guest_exists "$CT_ID"; then draft_forget CT_ID; bad "ID $CT_ID je već zauzet."; continue; fi
    break
  done
  ask_req CT_HOSTNAME "Hostname LXC-a:" jellyfin

  local stores=() tstores=() defs="" sinfo st sd sfree
  mapfile -t stores  < <(pvesm status --content rootdir 2>/dev/null | awk 'NR>1 && $3=="active"{print $1}')
  mapfile -t tstores < <(pvesm status --content vztmpl  2>/dev/null | awk 'NR>1 && $3=="active"{print $1}')
  ((${#stores[@]}))  || die "Nema storagea koji podržava LXC diskove (rootdir)."
  ((${#tstores[@]})) || die "Nema storagea za LXC predloške (vztmpl)."
  items=()
  for s in "${stores[@]}"; do
    sinfo=$(storage_dev "$s"); st=${sinfo%%|*}; sd=${sinfo#*|}
    sfree=$(pvesm status 2>/dev/null | awk -v s="$s" '$1==s{printf "%.0f GB slobodno", $6/1048576}')
    items+=("$s" "$(printf '%-8s %-22s %-16s%s' "$st" "$sd" "$sfree" "$([[ $sd == *nvme* ]] && echo '  ★ NVMe' || true)")")
    if [[ -z $defs && $sd == *nvme* && $st == lvmthin ]]; then defs=$s; fi
  done
  if [[ -z $defs ]]; then for s in "${stores[@]}"; do if [[ $(storage_dev "$s") == *nvme* ]]; then defs=$s; break; fi; done; fi
  if [[ -z $defs ]]; then defs=${stores[0]}; for s in "${stores[@]}"; do [[ $s == local-lvm ]] && defs=local-lvm; done; fi
  ask_menu CT_STORAGE "Storage za disk LXC-a.

Na njemu će biti baza, metapodaci i privremene datoteke transkodiranja – NVMe je najbolji izbor." "$defs" "${items[@]}"
  if [[ $(storage_dev "$CT_STORAGE") != *nvme* ]]; then
    ask_yn "$CT_STORAGE nije na NVMe disku – baza i transkodiranje bit će sporiji.

Nastaviti s njim?" y YN_NOT_NVME || { CT_STORAGE=$defs; ok "Storage promijenjen na $CT_STORAGE"; }
  fi
  if ((${#tstores[@]} == 1)); then CT_TPL_STORE=${tstores[0]}
  else
    items=(); for s in "${tstores[@]}"; do items+=("$s" "predlošci (vztmpl)"); done
    defs=${tstores[0]}; for s in "${tstores[@]}"; do [[ $s == local ]] && defs=local; done
    ask_menu CT_TPL_STORE "Storage za Debian predložak:" "$defs" "${items[@]}"
  fi

  ask_num CT_DISK  "Veličina diska (GB):

Jellyfin podaci zauzimaju ~${gb} GB; prijedlog uključuje mjesto za rast i transkodiranje." "$sugg"
  ask_num CT_CORES "Broj CPU jezgri:" 4
  ask_num CT_RAM   "RAM (MB):" 4096
  ask_num CT_SWAP  "Swap (MB):" 512

  local bridges=()
  mapfile -t bridges < <(ls /sys/class/net 2>/dev/null | grep -E '^vmbr' || true)
  if ((${#bridges[@]} > 1)); then
    items=(); for s in "${bridges[@]}"; do items+=("$s" "$(ip -br -4 addr show "$s" 2>/dev/null | awk '{print $3}')"); done
    ask_menu CT_BRIDGE "Mrežni bridge:" vmbr0 "${items[@]}"
  elif ((${#bridges[@]} == 1)); then CT_BRIDGE=${bridges[0]}
  else ask_req CT_BRIDGE "Mrežni bridge:" vmbr0; fi
  ask CT_VLAN "VLAN tag (prazno = bez VLAN-a):" ""

  ask_menu IP_MODE "IP adresa LXC-a:" dhcp \
    dhcp   "Automatski (DHCP) – adresu dodjeljuje router" \
    static "Statička adresa"
  CT_GW=""
  if [[ $IP_MODE == dhcp ]]; then CT_IP=dhcp
  else
    while :; do
      ask_req CT_IP "Statička IP adresa u CIDR obliku (npr. 192.168.1.50/24):" ""
      [[ $CT_IP =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$ ]] && break
      draft_forget CT_IP; bad "Neispravan format – upiši npr. 192.168.1.50/24"
    done
    ask_req CT_GW "Gateway:" "$(echo "${CT_IP%/*}" | awk -F. '{print $1"."$2"."$3".1"}')"
  fi
  ask CT_DNS "DNS server (prazno = isti kao Proxmox host):" ""

  ask_menu CT_TYPE "Vrsta LXC-a:" unpriv \
    unpriv "Neprivilegirani – sigurniji; NFS se montira na hostu (preporučeno)" \
    priv   "Privilegirani – NFS unutar LXC-a; slabija izolacija od hosta"
  if [[ $CT_TYPE == unpriv ]]; then CT_UNPRIV=1; else CT_UNPRIV=0; fi
  ask_secret CT_PASSWORD "Root lozinka za novi LXC:"
  CT_SSHKEYS=""
  if [[ -s /root/.ssh/authorized_keys ]] && ask_yn "Kopirati SSH ključeve s ovog Proxmox hosta (/root/.ssh/authorized_keys) u LXC?" y YN_SSHKEYS; then
    CT_SSHKEYS=/root/.ssh/authorized_keys
  fi
  if ask_yn "Automatski pokretati LXC pri bootu hosta?" y YN_ONBOOT; then CT_ONBOOT=1; else CT_ONBOOT=0; fi

  # ---------------------------------------------------------------- GPU i Jellyfin
  CUR_SECTION="3/4 · Grafika i Jellyfin"
  nv_detect; intel_detect
  local nvdrv; nvdrv=$(host_driver_version)
  if [[ -n $NV_ADDR && -n $INTEL_RENDER_DET ]]; then
    ask_menu GPU_MODE "Na hostu su dvije grafike. Koju koristi ovaj Jellyfin?" nvidia \
      nvidia "NVIDIA NVENC  · ${NV_NAME#NVIDIA Corporation }${nvdrv:+ · drajver $nvdrv}" \
      intel  "Intel QSV     · ${INTEL_NAME#Intel Corporation } · NVIDIA ostaje slobodna"
  elif [[ -n $NV_ADDR ]]; then GPU_MODE=nvidia
  elif [[ -n $INTEL_RENDER_DET ]]; then GPU_MODE=intel
  else die "Na ovom hostu nije pronađen ni NVIDIA GPU ni Intel iGPU."; fi
  INTEL_RENDER=$INTEL_RENDER_DET
  ok "Grafika: $([[ $GPU_MODE == nvidia ]] && echo "NVIDIA ($NV_NAME)" || echo "Intel QSV ($INTEL_NAME)")"

  ask_menu JF_VERSION_MODE "Koju verziju Jellyfina instalirati u LXC?" match \
    match  "Istu kao u Dockeru ($SRC_VERSION) – najsigurnija migracija" \
    latest "Najnoviju iz repozitorija – baza se automatski nadogradi"
  if ask_yn "Automatski uključiti hardversko transkodiranje u Jellyfinu?

Postavke se prilagode odabranoj grafici (enkoder, HW dekoderi, tone mapping)." y YN_HWACCEL; then APPLY_HWACCEL=1; else APPLY_HWACCEL=0; fi

  ask_menu OLD_MODE "Stari Docker Jellyfin se nakratko zaustavlja tijekom izvoza (konzistentna baza).

Što s njim nakon toga?" stop \
    stop     "Ostaje zaustavljen – novi LXC ga zamjenjuje" \
    parallel "Ponovno se pokreće – oba servera rade paralelno"
  if [[ $OLD_MODE == parallel ]]; then
    DISABLE_OLD=0
    if ask_yn "Dva servera s istim Server ID-om aplikacije (Xbox, mobitel, Seerr…) vide kao isti server i miješaju ih.

Generirati NOVI Server ID za novi Jellyfin? (preporučeno)" y YN_NEW_SERVER_ID; then NEW_SERVER_ID=1; else NEW_SERVER_ID=0; fi
    ask JF_SERVER_NAME "Ime novog servera (prikazuje se u aplikacijama; prazno = isto kao staro):" "Jellyfin-$CT_HOSTNAME"
  else
    DISABLE_OLD=1; NEW_SERVER_ID=0; JF_SERVER_NAME=""
  fi
  NV_VERSION=""

  # ---------------------------------------------------------------- sažetak
  CUR_SECTION="4/4 · Sažetak"
  local sum mounts=""
  for idx in "${!M_NFS[@]}"; do mounts+="
   ${M_NFS[$idx]}  ➜  ${M_LXC[$idx]}$( ((MEDIA_RO)) && echo '  (ro)' || true)"; done
  sum="IZVOR
   $SSH_USER@$DOCKER_HOST_IP · kontejner '$CONTAINER' · Jellyfin $SRC_VERSION
   $CONFIG_SRC

MEDIJI (NFS)${mounts:-
   (nema)}
   UID/GID: $MEDIA_UID/$MEDIA_GID

NOVI LXC $CT_ID '$CT_HOSTNAME'
   $( ((CT_UNPRIV)) && echo neprivilegirani || echo privilegirani ) · ${CT_CORES} CPU · ${CT_RAM} MB RAM · ${CT_DISK} GB na $CT_STORAGE
   mreža: $CT_BRIDGE${CT_VLAN:+ VLAN $CT_VLAN} · $CT_IP${CT_GW:+ · gw $CT_GW}

JELLYFIN
   verzija: $( [[ $JF_VERSION_MODE == match ]] && echo "ista ($SRC_VERSION)" || echo najnovija )
   grafika: $( [[ $GPU_MODE == nvidia ]] && echo "NVIDIA NVENC" || echo "Intel QSV ($INTEL_RENDER)" ) $( ((APPLY_HWACCEL)) && echo '· automatski uključeno' || true )
   stari server: $( ((DISABLE_OLD)) && echo 'ostaje zaustavljen' || echo "radi paralelno$( ((NEW_SERVER_ID)) && echo ' · novi Server ID' || true)${JF_SERVER_NAME:+ · ime '$JF_SERVER_NAME'}" )

Docker Jellyfin će biti ZAUSTAVLJEN tijekom izvoza.
POKRENI = kreni s migracijom · UREDI = prođi pitanja ponovno (odgovori ostaju ponuđeni)"
  local rc
  if (( USE_TUI )); then
    while :; do
      wt --scrolltext --yes-button "Pokreni" --no-button "Uredi" --yesno "$sum" "$(wt_h "$sum" 6)" "$(wt_w)" && rc=0 || rc=$?
      case $rc in 0) SUMMARY_OK=1; return 0 ;; 1) SUMMARY_OK=0; return 0 ;; *) wt_cancel ;; esac
    done
  elif (( API_MODE )); then
    local sc
    ask_menu sc "$sum" start start "Pokreni migraciju" edit "Uredi odgovore"
    if [[ $sc == start ]]; then SUMMARY_OK=1; else SUMMARY_OK=0; fi
  else
    echo "$sum" >&4
    local sc
    ask_menu sc "Što dalje?" start start "Pokreni migraciju" edit "Uredi odgovore"
    if [[ $sc == start ]]; then SUMMARY_OK=1; else SUMMARY_OK=0; fi
  fi
  return 0
}

# ============================================================================= KORAK 1: GPU NA HOSTU
gpu_detect() {
  nv_detect
  [[ -n $NV_ADDR ]] || die "NVIDIA GPU nije pronađen na Proxmox hostu."
  GPU_ADDR=$NV_ADDR; GPU_SLOT=${GPU_ADDR%.*}; GPU_NAME=$NV_NAME
  GPU_DRV=$(lspci -k -s "$GPU_ADDR" | awk -F': ' '/Kernel driver in use/{print $2}' || true)
}
blacklist_nouveau() {
  if [[ ! -f /etc/modprobe.d/blacklist-nouveau.conf ]]; then
    printf 'blacklist nouveau\noptions nouveau modeset=0\n' > /etc/modprobe.d/blacklist-nouveau.conf
    ok "nouveau stavljen na blacklist."
  fi
}
update_boot() {
  msg_info "Ažuriram initramfs i bootloader…"
  run update-initramfs -u -k all
  if [[ -s /etc/kernel/proxmox-boot-uuids ]] && command -v proxmox-boot-tool >/dev/null; then run proxmox-boot-tool refresh
  elif [[ -f /etc/default/grub ]]; then run update-grub || true; fi
  ok "initramfs i bootloader ažurirani."
}
release_vfio() {
  warn "$GPU_NAME je vezan na vfio-pci (PCI passthrough u VM) – za LXC ga mora koristiti host."
  local short=${GPU_SLOT#0000:} f vmid vname line key re
  re="^hostpci[0-9]+:[[:space:]]*(0000:)?${short}(\.[0-7])?([,;]|$)"
  for f in /etc/pve/qemu-server/*.conf; do
    [[ -e $f ]] || continue
    vmid=$(basename "$f" .conf); vname=$(awk -F': ' '/^name:/{print $2; exit}' "$f")
    while IFS= read -r line; do
      key=${line%%:*}
      if [[ $line =~ $re ]]; then
        if ask_yn "VM $vmid ($vname) ima ovu grafiku:
   $line

Docker kontejneri u tom VM-u koji koriste GPU neće se moći pokrenuti bez nje.

Ukloniti $key iz VM $vmid?" y; then
          run qm set "$vmid" --delete "$key" && ok "Uklonjen $key iz VM $vmid (primjenjuje se nakon restarta)."
        else
          die "GPU ne može istovremeno biti u VM-u i u LXC-u."
        fi
      elif [[ $line =~ ^hostpci[0-9]+:.*mapping= ]]; then
        warn "VM $vmid ($vname) koristi PCI mapping ($line) – provjeri ručno je li to $GPU_NAME."
      fi
    done < <(sed '/^\[/q' "$f")
  done
  local ids=() files=() id fl
  mapfile -t ids < <(lspci -n -s "$GPU_SLOT" | awk '{print $3}')
  for fl in /etc/modprobe.d/*.conf /etc/kernel/cmdline /etc/default/grub; do
    [[ -f $fl ]] || continue
    for id in "${ids[@]}"; do if grep -qi "$id" "$fl"; then files+=("$fl"); break; fi; done
  done
  for fl in "${files[@]}"; do
    RMIDS="${ids[*]}" perl -0pi.jfmig.bak -e 'BEGIN{%rm=map{lc($_)=>1}split / /,$ENV{RMIDS}}
      s{((?:vfio[-_]pci\.)?ids=)([0-9A-Fa-f:,]+)}{my($p,$l)=($1,$2);my @k=grep{!$rm{lc $_}}split /,/,$l;@k?$p.join(",",@k):""}ge' "$fl"
    sed -i -E '/^[[:space:]]*options[[:space:]]+vfio[-_]pci[[:space:]]*$/d' "$fl"
    ok "Uklonjeni ${ids[*]} iz $fl (backup: $fl.jfmig.bak)"
  done
  for fl in /etc/modprobe.d/*.conf; do
    [[ -f $fl ]] || continue
    if grep -qE '^[[:space:]]*(blacklist[[:space:]]+nvidia([[:space:]]|$|_drm|_modeset|_uvm)|softdep[[:space:]]+nvidia)' "$fl"; then
      sed -i.jfmig.bak -E '/^[[:space:]]*blacklist[[:space:]]+nvidia([[:space:]]|$|_drm|_modeset|_uvm)/d; /^[[:space:]]*softdep[[:space:]]+nvidia/d' "$fl"
      ok "Uklonjen blacklist/softdep za nvidia iz $fl"
    fi
  done
}
setup_host_nvidia_persist() {
  # moduli i /dev/nvidia* moraju postojati PRIJE starta LXC-a (i nakon svakog boota)
  [[ -f /etc/modules-load.d/nvidia.conf ]] || printf 'nvidia\nnvidia_uvm\n' > /etc/modules-load.d/nvidia.conf
  cat > /usr/local/sbin/jf-nvidia-nodes <<'EOF'
#!/bin/sh
# kreira /dev/nvidia* (uključujući nvidia-uvm) za prosljeđivanje u LXC
modprobe nvidia 2>/dev/null; modprobe nvidia_uvm 2>/dev/null
command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1
if command -v nvidia-modprobe >/dev/null; then
  nvidia-modprobe -c0 -u 2>/dev/null
else
  m=$(awk '$2=="nvidia-frontend"||$2=="nvidia"{print $1; exit}' /proc/devices)
  if [ -n "$m" ]; then
    [ -e /dev/nvidiactl ] || mknod -m 666 /dev/nvidiactl c "$m" 255
    [ -e /dev/nvidia0 ]   || mknod -m 666 /dev/nvidia0 c "$m" 0
  fi
  u=$(awk '$2=="nvidia-uvm"{print $1; exit}' /proc/devices)
  if [ -n "$u" ]; then
    [ -e /dev/nvidia-uvm ]       || mknod -m 666 /dev/nvidia-uvm c "$u" 0
    [ -e /dev/nvidia-uvm-tools ] || mknod -m 666 /dev/nvidia-uvm-tools c "$u" 1
  fi
fi
chmod 0666 /dev/nvidia* 2>/dev/null
exit 0
EOF
  chmod 755 /usr/local/sbin/jf-nvidia-nodes
  cat > /etc/systemd/system/jf-nvidia-nodes.service <<'EOF'
[Unit]
Description=NVIDIA device nodes for LXC passthrough
Before=pve-guests.service
After=systemd-modules-load.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/jf-nvidia-nodes

[Install]
WantedBy=multi-user.target
EOF
  run systemctl daemon-reload
  run systemctl enable jf-nvidia-nodes.service || true
  /usr/local/sbin/jf-nvidia-nodes
  [[ -c /dev/nvidia0 && -c /dev/nvidiactl ]] || die "/dev/nvidia0 ne postoji – NVIDIA drajver na hostu ne radi."
  if [[ -c /dev/nvidia-uvm ]]; then ok "/dev/nvidia* uređaji spremni (i nakon svakog boota)."
  else warn "/dev/nvidia-uvm ne postoji – NVENC možda neće raditi."; fi
}
install_host_driver() {
  if command -v mokutil >/dev/null && mokutil --sb-state 2>/dev/null | grep -qi enabled; then
    ask_yn "Secure Boot je uključen – nepotpisani NVIDIA modul se neće učitati.

Svejedno nastaviti?" n || die "Isključi Secure Boot ili ručno potpiši modul."
  fi
  if [[ -z ${NV_VERSION:-} ]]; then
    local det; det=$(latest_driver_for_gpu)
    ask_req NV_VERSION "NVIDIA drajver nije instaliran na hostu.
$( ((NV_LEGACY)) && echo "
$GPU_NAME podržavaju grane drajvera do ${LEGACY_BRANCH}.x." || true)
Popis verzija: https://download.nvidia.com/XFree86/Linux-x86_64/

Verzija NVIDIA drajvera:" "$det"
  fi
  if (( NV_LEGACY && ${NV_VERSION%%.*} > LEGACY_BRANCH )); then
    ask_yn "Grana ${NV_VERSION%%.*} vjerojatno NE podržava $GPU_NAME. Svejedno nastaviti?" n || die "Prekinuto."
  fi
  save_answers
  msg_info "Instaliram kernel headere i alate za izgradnju modula…"
  run apt-get update -qq
  run apt-get install -y -qq "proxmox-headers-$(uname -r)" \
    || run apt-get install -y -qq "pve-headers-$(uname -r)" \
    || die "Ne mogu instalirati kernel headere za $(uname -r) (provjeri pve-no-subscription repozitorij)."
  run apt-get install -y -qq proxmox-default-headers || run apt-get install -y -qq pve-headers || true
  run apt-get install -y -qq build-essential dkms pkg-config
  ok "Kernel headeri i alati instalirani."
  dl_nvidia_run "$NV_VERSION"
  local kmt=()
  (( NV_LEGACY && ${NV_VERSION%%.*} >= 560 )) && kmt=(--kernel-module-type=proprietary)   # Maxwell/Pascal nemaju open modul
  ( trap - ERR; set +e; sh "$NV_RUN" --silent --dkms "${kmt[@]}" >>"$LOG" 2>&1 ) &
  spin_wait $! "Instaliram NVIDIA $NV_VERSION na host (DKMS)" || die "NVIDIA instalacija nije uspjela – vidi /var/log/nvidia-installer.log"
  run update-initramfs -u -k all || true
  ok "NVIDIA $NV_VERSION instaliran."
  setup_host_nvidia_persist
}
phase_gpu_host() {
  if [[ $GPU_MODE == intel ]]; then
    hdr "Korak 1/4 · Intel iGPU na hostu"
    intel_detect
    [[ -n $INTEL_RENDER_DET ]] || die "Intel render uređaj (/dev/dri/renderD*) ne postoji – je li iGPU uključen u BIOS-u?"
    INTEL_RENDER=$INTEL_RENDER_DET
    ok "Intel iGPU: $INTEL_NAME → $INTEL_RENDER"
    return
  fi
  hdr "Korak 1/4 · NVIDIA na hostu"
  gpu_detect
  ok "GPU: $GPU_NAME (kernel drajver: ${GPU_DRV:-nijedan})"
  local hv=""
  if [[ $GPU_DRV == nvidia ]] && hv=$(host_driver_version) && [[ -n $hv ]]; then
    if ! command -v nvidia-smi >/dev/null; then
      msg_info "Instaliram nvidia-smi i nvidia-modprobe…"
      if run apt-get install -y -qq nvidia-smi nvidia-modprobe; then ok "nvidia-smi instaliran."
      else warn "nvidia-smi/nvidia-modprobe nisu instalirani – koristim rezervni način za /dev/nvidia*."; fi
    fi
    NV_VERSION=$hv
    ok "NVIDIA drajver $hv već radi na hostu."
    setup_host_nvidia_persist
    return
  fi
  local need_reboot=0
  case $GPU_DRV in
    vfio-pci) release_vfio; need_reboot=1 ;;
    nouveau)  need_reboot=1 ;;
  esac
  blacklist_nouveau
  if lsmod | grep -q '^nouveau'; then need_reboot=1; fi
  if (( need_reboot )); then
    update_boot
    save_answers
    if ask_yn "Potreban je RESTART Proxmox hosta da se grafika oslobodi.
(Restartat će se i svi VM-ovi i LXC-ovi.)

Nakon restarta pokreni:
   bash $SELF --resume

Restartati host sada?" n; then sync; reboot; fi
    info "Nakon restarta pokreni:  ${B}bash $SELF --resume${N}"
    exit 0
  fi
  install_host_driver
}

# ============================================================================= KORAK 2: IZVOZ
phase_export() {
  hdr "Korak 2/4 · Izvoz iz Dockera"
  ssh_connect
  ensure_sudo_pw
  local qc running rc=0
  qc=$(printf %q "$CONTAINER")
  running=$(rsudo "docker inspect -f '{{.State.Running}}' $qc")
  if [[ $running == true ]]; then
    msg_info "Zaustavljam Docker kontejner '$CONTAINER'…"
    rsudo "docker stop -t 60 $qc" >>"$LOG" 2>&1
    ok "Docker kontejner '$CONTAINER' zaustavljen."
  fi
  if (( DISABLE_OLD )); then
    rsudo "docker update --restart=no $qc" >>"$LOG" 2>&1 && ok "Auto-restart starog kontejnera isključen."
  fi
  ( trap - ERR; set +e
    rsudo "tar -C $(printf %q "$CONFIG_SRC") --exclude=./cache --exclude=./transcodes --exclude=./log --exclude=./logs \
           --exclude=./data/transcodes --exclude=./data/cache -czf - ." > "$TARBALL.part" 2>>"$LOG" ) &
  spin_wait $! "Pakiram i prenosim Jellyfin podatke" "$TARBALL.part" || rc=$?
  (( rc <= 1 )) || die "Izvoz nije uspio (tar/ssh kod $rc)."
  mv "$TARBALL.part" "$TARBALL"
  ( trap - ERR; tar -tzf "$TARBALL" > "$STATE_DIR/tar-list.txt" ) &
  spin_wait $! "Provjeravam arhivu" || die "Arhiva je oštećena."
  grep -qE '(^|/)(jellyfin|library)\.db$' "$STATE_DIR/tar-list.txt" || die "U arhivi nema Jellyfin baze – je li $CONFIG_SRC ispravan?"
  ok "Izvezeno $(du -h "$TARBALL" | cut -f1) → $TARBALL"
  if (( ! DISABLE_OLD )) && [[ $running == true ]]; then
    rsudo "docker start $qc" >>"$LOG" 2>&1 && ok "Stari Jellyfin ponovno pokrenut (radi paralelno)."
  fi
}

# ============================================================================= KORAK 3: LXC
pick_template() {
  local v t
  for v in 12 13; do
    t=$(pveam list "$CT_TPL_STORE" 2>/dev/null | awk -v p="debian-$v-standard" '$1 ~ p {print $1}' | sort -V | tail -n1 || true)
    if [[ -n $t ]]; then TEMPLATE=$t; ok "Predložak: ${t##*/}"; return; fi
  done
  msg_info "Preuzimam Debian predložak…"
  run pveam update || true
  for v in 12 13; do
    t=$(pveam available --section system 2>/dev/null | awk -v p="debian-$v-standard" '$2 ~ p {print $2}' | sort -V | tail -n1 || true)
    if [[ -n $t ]]; then run pveam download "$CT_TPL_STORE" "$t"; TEMPLATE="$CT_TPL_STORE:vztmpl/$t"; ok "Predložak preuzet: $t"; return; fi
  done
  die "Debian predložak nije pronađen."
}
idmap_lines() {   # idmap_lines u|g ID
  local t=$1 id=$2
  if (( id <= 0 || id > 65535 )); then echo "lxc.idmap: $t 0 100000 65536"; return 0; fi
  echo "lxc.idmap: $t 0 100000 $id"
  echo "lxc.idmap: $t $id $id 1"
  if (( 65535 - id > 0 )); then echo "lxc.idmap: $t $((id + 1)) $((100001 + id)) $((65535 - id))"; fi
  return 0
}
configure_gpu_passthrough() {
  local conf="/etc/pve/lxc/$CT_ID.conf" devs=() d i=0 pvev dopt=""
  if [[ $GPU_MODE == intel ]]; then
    [[ -c $INTEL_RENDER ]] || die "$INTEL_RENDER ne postoji na hostu."
    devs=("$INTEL_RENDER"); dopt=",mode=0666"
  else
    [[ -x /usr/local/sbin/jf-nvidia-nodes ]] && /usr/local/sbin/jf-nvidia-nodes
    for d in /dev/nvidia0 /dev/nvidiactl /dev/nvidia-modeset /dev/nvidia-uvm /dev/nvidia-uvm-tools \
             /dev/nvidia-caps/nvidia-cap1 /dev/nvidia-caps/nvidia-cap2; do
      if [[ -c $d ]]; then devs+=("$d"); fi
    done
  fi
  pvev=$(pveversion | grep -oP 'pve-manager/\K[0-9]+\.[0-9]+' || echo 0)
  if dpkg --compare-versions "$pvev" ge 8.1; then
    for d in "${devs[@]}"; do run pct set "$CT_ID" "--dev$i" "$d$dopt"; i=$((i + 1)); done
  else
    local majors=()
    mapfile -t majors < <(for d in "${devs[@]}"; do printf '%d\n' "0x$(stat -c %t "$d")"; done | sort -un)
    for d in "${majors[@]}"; do echo "lxc.cgroup2.devices.allow: c $d:* rwm" >> "$conf"; done
    for d in "${devs[@]}"; do echo "lxc.mount.entry: $d ${d#/} none bind,optional,create=file" >> "$conf"; done
  fi
  ok "Grafika proslijeđena u LXC: ${devs[*]}"
}
configure_host_nfs() {   # neprivilegirani: NFS na hostu + bind mount
  local i hdir opts ro
  ((${#M_NFS[@]})) || return 0
  for i in "${!M_NFS[@]}"; do
    hdir="/mnt/jellyfin-nfs/ct${CT_ID}${M_LXC[$i]//\//_}"
    opts="rw,hard,noatime,_netdev,nofail"; ro=""
    if (( MEDIA_RO )); then opts="ro,hard,noatime,_netdev,nofail"; ro=",ro=1"; fi
    msg_info "Montiram ${M_NFS[$i]}…"
    mkdir -p "$hdir"
    if ! grep -qE "^[^#]*[[:space:]]${hdir}[[:space:]]" /etc/fstab; then
      printf '# jellyfin-migration CT %s -> %s\n%s %s nfs %s 0 0\n' "$CT_ID" "${M_LXC[$i]}" "${M_NFS[$i]}" "$hdir" "$opts" >> /etc/fstab
    fi
    run systemctl daemon-reload
    mountpoint -q "$hdir" || run mount "$hdir" || die "Montiranje ${M_NFS[$i]} na $hdir nije uspjelo."
    run pct set "$CT_ID" "--mp$i" "$hdir,mp=${M_LXC[$i]}$ro"
    ok "NFS ${M_NFS[$i]} ➜ LXC:${M_LXC[$i]}"
  done
  mkdir -p /etc/systemd/system/pve-guests.service.d
  printf '[Unit]\nWants=remote-fs.target\nAfter=remote-fs.target\n' > /etc/systemd/system/pve-guests.service.d/10-jellyfin-nfs.conf
  run systemctl daemon-reload
}
phase_create_lxc() {
  hdr "Korak 3/4 · Novi LXC $CT_ID"
  guest_exists "$CT_ID" && die "Gost s ID-om $CT_ID već postoji. Obriši ga ili pokreni migraciju ispočetka s drugim ID-om."
  pick_template
  local net="name=eth0,bridge=$CT_BRIDGE,ip=$CT_IP"
  [[ $CT_IP != dhcp && -n $CT_GW ]] && net+=",gw=$CT_GW"
  [[ -n $CT_VLAN ]] && net+=",tag=$CT_VLAN"
  local args=(--hostname "$CT_HOSTNAME" --cores "$CT_CORES" --memory "$CT_RAM" --swap "$CT_SWAP"
              --rootfs "$CT_STORAGE:$CT_DISK" --net0 "$net" --ostype debian --onboot "$CT_ONBOOT"
              --unprivileged "$CT_UNPRIV" --tags jellyfin --timezone host --start 0)
  if (( CT_UNPRIV )); then args+=(--features nesting=1); else args+=(--features "nesting=1,mount=nfs"); fi
  [[ -n $CT_DNS ]] && args+=(--nameserver "$CT_DNS")
  [[ -n $CT_SSHKEYS ]] && args+=(--ssh-public-keys "$CT_SSHKEYS")
  msg_info "Kreiram LXC $CT_ID na $CT_STORAGE…"
  log "RUN: pct create $CT_ID $TEMPLATE ${args[*]} --password ***"
  pct create "$CT_ID" "$TEMPLATE" "${args[@]}" --password "$CT_PASSWORD" >>"$LOG" 2>&1
  ok "LXC $CT_ID '$CT_HOSTNAME' kreiran (${CT_DISK} GB na $CT_STORAGE)."

  if (( CT_UNPRIV )) && (( MEDIA_UID > 0 || MEDIA_GID > 0 )); then
    { idmap_lines u "$MEDIA_UID"; idmap_lines g "$MEDIA_GID"; } >> "/etc/pve/lxc/$CT_ID.conf"
    if (( MEDIA_UID > 0 )) && ! grep -qx "root:$MEDIA_UID:1" /etc/subuid; then echo "root:$MEDIA_UID:1" >> /etc/subuid; fi
    if (( MEDIA_GID > 0 )) && ! grep -qx "root:$MEDIA_GID:1" /etc/subgid; then echo "root:$MEDIA_GID:1" >> /etc/subgid; fi
    ok "UID $MEDIA_UID / GID $MEDIA_GID mapirani 1:1 prema hostu (NFS prava)."
  fi
  configure_gpu_passthrough
  (( CT_UNPRIV )) && configure_host_nfs

  msg_info "Pokrećem LXC i čekam mrežu…"
  run pct start "$CT_ID"
  local i
  for i in $(seq 1 30); do
    if pct exec "$CT_ID" -- getent hosts deb.debian.org >/dev/null 2>&1; then
      ok "LXC radi, IP: $(pct exec "$CT_ID" -- hostname -I 2>/dev/null | awk '{print $1}')"; return
    fi
    sleep 2
  done
  die "LXC nema mrežu/DNS (provjeri bridge, VLAN, IP)."
}

# ============================================================================= KORAK 4: UNUTAR LXC-a
# Skripta u LXC-u javlja napredak redovima @@STEP/@@OK/@@WARN/@@FAIL; sve ostalo ide u log.
write_inner_script() {
cat > "$STATE_DIR/jf-inner.sh" <<'INNER'
#!/usr/bin/env bash
set -Eeuo pipefail
source /root/jf-migrate.env
export DEBIAN_FRONTEND=noninteractive
say() { echo "@@STEP $*"; }
okk() { echo "@@OK $*"; }
wrn() { echo "@@WARN $*"; }
die() { echo "@@FAIL $*"; exit 1; }
trap 'echo "@@FAIL Greška u LXC skripti, linija $LINENO: $BASH_COMMAND"' ERR
FF=/usr/lib/jellyfin-ffmpeg/ffmpeg

# ---------------------------------------------------------------- paketi
say "Ažuriram Debian i instaliram osnovne pakete…"
apt-get update -qq
apt-get install -y -qq curl gnupg ca-certificates rsync xmlstarlet jq procps
if ((${#NFS_LINES[@]})); then apt-get install -y -qq nfs-common; fi
okk "Osnovni paketi instalirani."

# ---------------------------------------------------------------- grafika
if [[ $GPU_MODE == nvidia ]]; then
  if nvidia-smi >/dev/null 2>&1 && [[ "$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1)" == "$NV_VERSION" ]]; then
    okk "NVIDIA $NV_VERSION userspace već instaliran."
  else
    say "Instaliram NVIDIA $NV_VERSION knjižnice (bez kernel modula)…"
    sh /root/NVIDIA.run --silent --no-kernel-module --no-nouveau-check --no-x-check \
      || sh /root/NVIDIA.run --silent --no-kernel-modules --no-nouveau-check --no-x-check \
      || die "NVIDIA installer nije uspio (/var/log/nvidia-installer.log u LXC-u)."
  fi
  rm -f /root/NVIDIA.run
  nvidia-smi -L || die "nvidia-smi ne vidi GPU unutar LXC-a."
  okk "NVIDIA u LXC-u: $(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader)"
else
  say "Instaliram Intel OpenCL (HDR tone mapping)…"
  apt-get install -y -qq intel-opencl-icd || wrn "intel-opencl-icd nije instaliran – HDR tone mapping možda neće raditi."
  [[ -c $INTEL_RENDER ]] || die "$INTEL_RENDER ne postoji unutar LXC-a."
  okk "Intel iGPU dostupan u LXC-u: $INTEL_RENDER"
fi

# ---------------------------------------------------------------- Jellyfin repo + instalacija
say "Dodajem službeni Jellyfin repozitorij…"
. /etc/os-release
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key | gpg --dearmor --yes -o /etc/apt/keyrings/jellyfin.gpg
cat > /etc/apt/sources.list.d/jellyfin.sources <<EOF
Types: deb
URIs: https://repo.jellyfin.org/${ID:-debian}
Suites: ${VERSION_CODENAME:-bookworm}
Components: main
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/jellyfin.gpg
EOF
apt-get update -qq

# "12.4.0" (server) i "12.4" (paket) su ista verzija → uspoređuj bez završnih ".0"
vnorm() { local v=${1#*:}; v=${v%%[+~-]*}; while [[ $v == *.*.0 ]]; do v=${v%.0}; done; echo "$v"; }
SRC_N=$(vnorm "$SRC_VERSION")
pick_ver() {
  local p v
  while read -r p; do
    v=$(vnorm "$p")
    if [[ $v == "$SRC_N" ]]; then echo "$p"; return 0; fi
  done < <(apt-cache madison "$1" | awk '{print $3}')
  return 0
}
echo "Dostupne verzije jellyfin-server: $(apt-cache madison jellyfin-server | awk '{print $3}' | head -n 8 | xargs)"
PKGS=(jellyfin); HOLD=0
if [[ $JF_VERSION_MODE == match ]]; then
  vs=$(pick_ver jellyfin-server); vw=$(pick_ver jellyfin-web)
  if [[ -n $vs && -n $vw ]]; then PKGS=("jellyfin-server=$vs" "jellyfin-web=$vw"); HOLD=1
  else wrn "Verzija $SRC_VERSION nije dostupna u repozitoriju – instaliram najnoviju."; fi
fi
if (( ! HOLD )); then
  cand=$(apt-cache policy jellyfin-server | awk '/Candidate:/{print $2}')
  [[ -n $cand && $cand != "(none)" ]] || die "jellyfin-server nije dostupan u repozitoriju."
  CAND_N=$(vnorm "$cand")
  if dpkg --compare-versions "$CAND_N" lt "$SRC_N"; then die "Repozitorij nudi $CAND_N, a baza je s novije verzije $SRC_VERSION – downgrade nije moguć."; fi
  if dpkg --compare-versions "$CAND_N" gt "$SRC_N"; then wrn "Nadogradnja $SRC_VERSION → $CAND_N: prvo pokretanje može potrajati (migracija baze)."; fi
fi
say "Instaliram Jellyfin…"
apt-get install -y -qq "${PKGS[@]}"
[[ -x $FF ]] || apt-get install -y -qq jellyfin-ffmpeg7 || apt-get install -y -qq jellyfin-ffmpeg6
systemctl stop jellyfin 2>/dev/null || true
okk "Jellyfin $(dpkg-query -W -f='${Version}' jellyfin-server) instaliran."

# ---------------------------------------------------------------- korisnik (UID/GID za NFS)
if (( MEDIA_UID > 0 )) && [[ $(id -u jellyfin) != "$MEDIA_UID" ]]; then
  other=$(getent passwd "$MEDIA_UID" | cut -d: -f1 || true)
  if [[ -n $other ]]; then wrn "UID $MEDIA_UID već koristi '$other' – UID jellyfina se ne mijenja."
  else usermod -u "$MEDIA_UID" jellyfin; fi
fi
if (( MEDIA_GID > 0 )); then
  gname=$(getent group "$MEDIA_GID" | cut -d: -f1 || true)
  if [[ -z $gname ]]; then groupmod -g "$MEDIA_GID" jellyfin
  elif [[ $gname != jellyfin ]]; then usermod -aG "$gname" jellyfin; fi
fi
for grp in video render; do if getent group "$grp" >/dev/null; then usermod -aG "$grp" jellyfin; fi; done
okk "Korisnik jellyfin: UID $(id -u jellyfin), GID $(id -g jellyfin)"

# ---------------------------------------------------------------- NFS (privilegirani LXC)
if ((${#NFS_LINES[@]})); then
  say "Montiram NFS unutar LXC-a…"
  for l in "${NFS_LINES[@]}"; do
    IFS='|' read -r src dest opts <<<"$l"
    mkdir -p "$dest"
    grep -qsE "^[^#]*[[:space:]]${dest}[[:space:]]" /etc/fstab || echo "$src $dest nfs $opts 0 0" >> /etc/fstab
  done
  systemctl daemon-reload
  mount -a -t nfs,nfs4 || die "NFS montiranje u LXC-u nije uspjelo (dopušta li NAS IP ovog LXC-a?)."
  mkdir -p /etc/systemd/system/jellyfin.service.d
  { echo "[Unit]"; for d in "${M_LXC[@]}"; do echo "RequiresMountsFor=$d"; done; } > /etc/systemd/system/jellyfin.service.d/10-media-mounts.conf
  systemctl daemon-reload
  okk "NFS montiran unutar LXC-a."
fi

# ---------------------------------------------------------------- vraćanje podataka
say "Vraćam bazu, korisnike, metapodatke i pluginove…"
SRC=/root/jfsrc
DB=$(find "$SRC" -maxdepth 4 -type f -name jellyfin.db -path '*/data/*' -print -quit)
[[ -n $DB ]] || DB=$(find "$SRC" -maxdepth 4 -type f -name library.db -path '*/data/*' -print -quit)
[[ -n $DB ]] || die "U izvezenim podacima nema Jellyfin baze."
DATA_DIR=$(dirname "$(dirname "$DB")")
SYSXML=$(find "$SRC" -maxdepth 3 -type f -name system.xml ! -path '*/plugins/*' -print -quit)
[[ -n $SYSXML ]] || die "U izvezenim podacima nema system.xml."
CONFIG_DIR=$(dirname "$SYSXML")
rel() { echo "/config${1#"$SRC"}"; }
DATA_INNER=$(rel "$DATA_DIR"); CONFIG_INNER=$(rel "$CONFIG_DIR")
echo "Docker raspored: data=$DATA_INNER config=$CONFIG_INNER cache=$CACHE_INNER"

mkdir -p /root/jellyfin-fresh-etc && cp -a /etc/jellyfin/. /root/jellyfin-fresh-etc/ 2>/dev/null || true
find /var/lib/jellyfin -mindepth 1 -delete
find /etc/jellyfin -mindepth 1 -maxdepth 1 ! -name 'logging.json' -exec rm -rf {} +

dex=(--exclude=/log --exclude=/logs --exclude=/cache --exclude=/transcodes)
cex=(--exclude=/log --exclude=/logs --exclude=/cache --exclude=/transcodes --exclude='/logging*.json'
     --exclude=/custom-cont-init.d --exclude=/custom-services.d --exclude='*.pid')
if [[ $CONFIG_DIR == "$DATA_DIR"/* ]]; then dex+=("--exclude=/${CONFIG_DIR#"$DATA_DIR"/}"); fi
if [[ $DATA_DIR == "$CONFIG_DIR"/* ]]; then cex+=("--exclude=/${DATA_DIR#"$CONFIG_DIR"/}"); fi
rsync -aH "${dex[@]}" "$DATA_DIR"/ /var/lib/jellyfin/
rsync -a "${cex[@]}" "$CONFIG_DIR"/ /etc/jellyfin/

# Docker putanje → native putanje u XML postavkama
declare -A MAP=()
MAP["$CONFIG_INNER"]=/etc/jellyfin
MAP["/config/log"]=/var/log/jellyfin
MAP["$CACHE_INNER"]=/var/cache/jellyfin
MAP["$DATA_INNER"]=/var/lib/jellyfin
mapfile -t XMLS < <(find /etc/jellyfin /var/lib/jellyfin/plugins/configurations -maxdepth 1 -type f -name '*.xml' 2>/dev/null)
while read -r from; do
  [[ -z $from ]] && continue
  to=${MAP[$from]}
  if ((${#XMLS[@]})); then sed -i -E "s#${from}(/|<|\"|$)#${to}\1#g" "${XMLS[@]}"; fi
done < <(for k in "${!MAP[@]}"; do echo "${#k} $k"; done | sort -rn | cut -d' ' -f2-)

# kompatibilni symlinkovi za eventualne apsolutne Docker putanje u bazi
mk_compat() {
  local from=$1 to=$2 p
  [[ -e $from || -L $from ]] && return 0
  p=$(dirname "$from"); while [[ $p != / ]]; do [[ -L $p ]] && return 0; p=$(dirname "$p"); done
  mkdir -p "$(dirname "$from")"; ln -s "$to" "$from"
}
mk_compat "$DATA_INNER" /var/lib/jellyfin
mk_compat "$CACHE_INNER" /var/cache/jellyfin

NET=/etc/jellyfin/network.xml
[[ -f $NET ]] && xmlstarlet ed -L -d '/NetworkConfiguration/LocalNetworkAddresses/*' "$NET" 2>/dev/null || true
okk "Podaci vraćeni, putanje prilagođene native instalaciji."

# ---------------------------------------------------------------- putanje koje su postojale samo u Dockeru
under_media() { local p=$1 d; for d in "${M_LXC[@]}"; do [[ $p == "$d" || $p == "${d%/}"/* ]] && return 0; done; return 1; }
under_native() { [[ $1 =~ ^/(var/lib/jellyfin|etc/jellyfin|var/cache/jellyfin|var/log/jellyfin|usr|dev|proc|sys|tmp)(/|$) ]]; }

# transkodiranje uvijek lokalno (brzi disk LXC-a), nikad na NAS-u ili Docker putanji
ENC=/etc/jellyfin/encoding.xml
if [[ -f $ENC ]]; then
  oldt=$(xmlstarlet sel -t -v '/EncodingOptions/TranscodingTempPath' "$ENC" 2>/dev/null || true)
  if [[ $(xmlstarlet sel -t -v 'count(/EncodingOptions/TranscodingTempPath)' "$ENC") -gt 0 ]]; then
    xmlstarlet ed -L -u /EncodingOptions/TranscodingTempPath -v /var/cache/jellyfin/transcodes "$ENC"
  else
    xmlstarlet ed -L -s /EncodingOptions -t elem -n TranscodingTempPath -v /var/cache/jellyfin/transcodes "$ENC"
  fi
  [[ -n $oldt && $oldt != /var/cache/jellyfin/transcodes ]] && okk "Transkodiranje: $oldt ➜ /var/cache/jellyfin/transcodes"
fi
mkdir -p /var/cache/jellyfin/transcodes

# ostale *Path postavke (system.xml, pluginovi) koje pokazuju izvan Jellyfina i NAS-a
mapfile -t XPATHS < <(grep -hoP '<(?!CertificatePath|EncoderAppPath)\w*Path>\K/[^<]+' \
    /etc/jellyfin/*.xml /var/lib/jellyfin/plugins/configurations/*.xml 2>/dev/null | sort -u || true)
for p in "${XPATHS[@]}"; do
  if under_native "$p" || under_media "$p"; then continue; fi
  if mkdir -p "$p" 2>/dev/null && chown jellyfin:jellyfin "$p"; then
    wrn "Postavka iz Dockera pokazuje na $p – mapa napravljena lokalno (provjeri u Dashboardu treba li drugdje)."
  else
    wrn "Postavka iz Dockera pokazuje na $p, a tu mapu ne mogu napraviti."
  fi
done

# ---------------------------------------------------------------- paralelni rad sa starim serverom
SYSX=/etc/jellyfin/system.xml
if (( NEW_SERVER_ID )); then
  rm -f /var/lib/jellyfin/data/device.txt   # Jellyfin pri startu generira novi Server ID
  okk "Novi Jellyfin dobit će vlastiti Server ID."
fi
if [[ -n ${JF_SERVER_NAME:-} && -f $SYSX ]]; then
  if [[ $(xmlstarlet sel -t -v 'count(/ServerConfiguration/ServerName)' "$SYSX") -gt 0 ]]; then
    xmlstarlet ed -L -u /ServerConfiguration/ServerName -v "$JF_SERVER_NAME" "$SYSX"
  else
    xmlstarlet ed -L -s /ServerConfiguration -t elem -n ServerName -v "$JF_SERVER_NAME" "$SYSX"
  fi
  okk "Ime servera: $JF_SERVER_NAME"
fi

# ---------------------------------------------------------------- hardversko transkodiranje
ENC=/etc/jellyfin/encoding.xml
xcount() { xmlstarlet sel -t -v "count(/EncodingOptions/$1)" "$ENC"; }
xset() {
  if [[ $(xcount "$1") -gt 0 ]]; then xmlstarlet ed -L -u "/EncodingOptions/$1" -v "$2" "$ENC"
  else xmlstarlet ed -L -s /EncodingOptions -t elem -n "$1" -v "$2" "$ENC"; fi
}
if (( APPLY_HWACCEL )); then
  say "Uključujem hardversko transkodiranje…"
  if [[ ! -f $ENC ]]; then
    cat > "$ENC" <<'EOF'
<?xml version="1.0" encoding="utf-8"?>
<EncodingOptions xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
</EncodingOptions>
EOF
  fi
  xset EnableHardwareEncoding true
  xset EnableDecodingColorDepth10Hevc true
  xset EnableDecodingColorDepth10Vp9 true
  xset EnableTonemapping true
  if [[ $GPU_MODE == nvidia ]]; then
    xset HardwareAccelerationType nvenc
    xset EnableEnhancedNvdecDecoder true
  else
    xset HardwareAccelerationType qsv
    xset VaapiDevice "$INTEL_RENDER"
    xset PreferSystemNativeHwDecoder true
    xset EnableVppTonemapping false
    xset EnableIntelLowPowerH264HwEncoder false   # low-power treba GuC/HuC firmware
    xset EnableIntelLowPowerHevcHwEncoder false
  fi
  xmlstarlet ed -L -d '/EncodingOptions/HardwareDecodingCodecs' "$ENC"
  xmlstarlet ed -L -s /EncodingOptions -t elem -n HardwareDecodingCodecs "$ENC"
  for cdc in $DEC_CODECS; do
    xmlstarlet ed -L -s /EncodingOptions/HardwareDecodingCodecs -t elem -n string -v "$cdc" "$ENC"
  done
  okk "HW transkodiranje: $([[ $GPU_MODE == nvidia ]] && echo NVENC || echo QSV) · dekoderi: $DEC_CODECS · tone mapping"
fi
if [[ -f $ENC ]]; then
  for e in EncoderAppPath EncoderAppPathDisplay; do
    if [[ $(xcount "$e") -gt 0 ]]; then xmlstarlet ed -L -u "/EncodingOptions/$e" -v "$FF" "$ENC"; fi
  done
fi

chown -R jellyfin:jellyfin /var/lib/jellyfin /etc/jellyfin /var/cache/jellyfin /var/log/jellyfin
rm -rf "$SRC"

# ---------------------------------------------------------------- start + provjere
say "Pokrećem Jellyfin (prvo pokretanje nakon migracije može potrajati)…"
systemctl enable jellyfin >/dev/null 2>&1 || true
JSTART=$(date '+%Y-%m-%d %H:%M:%S')
systemctl start jellyfin
up=0; fixed=()
for i in $(seq 1 180); do
  v=$(curl -fsS -m 3 http://127.0.0.1:8096/System/Info/Public 2>/dev/null | jq -r '.Version // empty' 2>/dev/null || true)
  if [[ -n $v ]]; then okk "Jellyfin $v radi."; up=1; break; fi
  # samopopravak: Jellyfin ne može pisati u neku putanju zaostalu iz Dockera
  badp=$(journalctl -u jellyfin --since "$JSTART" --no-pager 2>/dev/null \
         | grep -oP "(Access to the path|Could not find a part of the path) '\K[^']+" | tail -n1 || true)
  if [[ -n $badp && " ${fixed[*]} " != *" $badp "* && ${#fixed[@]} -lt 5 ]]; then
    under_media "$badp" && die "Jellyfin nema prava pisanja na NAS-u: $badp (provjeri NFS export i UID $(id -u jellyfin))."
    d=$badp; [[ ! -d $d && ${d##*/} == *.* ]] && d=$(dirname "$d")
    if [[ $d == / || ( $d =~ ^/(usr|etc|bin|sbin|lib|lib64|boot|dev|proc|sys|root|run)(/|$) && $d != /etc/jellyfin* ) ]]; then
      die "Jellyfin traži pristup sistemskoj putanji $badp – provjeri postavke ručno."
    fi
    systemctl stop jellyfin 2>/dev/null || true
    mkdir -p "$d" && chown -R jellyfin:jellyfin "$d"
    wrn "Jellyfin nije mogao pisati u $badp – mapa napravljena s pravima za jellyfin, ponovno pokrećem."
    fixed+=("$badp")
    JSTART=$(date '+%Y-%m-%d %H:%M:%S'); sleep 1
    systemctl reset-failed jellyfin 2>/dev/null || true
    systemctl start jellyfin
    continue
  fi
  if systemctl is-failed -q jellyfin; then journalctl -u jellyfin -n 60 --no-pager; die "Jellyfin servis se srušio (vidi log)."; fi
  sleep 5
done
(( up )) || wrn "Jellyfin se još pokreće – prati: journalctl -fu jellyfin"

say "Testiram hardversko kodiranje…"
if [[ $GPU_MODE == nvidia ]]; then
  for codec in h264_nvenc hevc_nvenc; do
    if runuser -u jellyfin -- "$FF" -hide_banner -v error -f lavfi -i testsrc2=size=1920x1080:rate=30 -t 3 -c:v "$codec" -f null - ; then
      okk "Test $codec: radi"
    else wrn "Test $codec nije uspio – provjeri /dev/nvidia* u LXC-u i verziju drajvera."; fi
  done
else
  VAINFO=/usr/lib/jellyfin-ffmpeg/vainfo
  if [[ -x $VAINFO ]] && runuser -u jellyfin -- "$VAINFO" --display drm --device "$INTEL_RENDER" >/dev/null 2>&1; then
    okk "VA-API: $(runuser -u jellyfin -- "$VAINFO" --display drm --device "$INTEL_RENDER" 2>/dev/null | grep -m1 -i 'driver version' | sed 's/.*: *//')"
  else wrn "vainfo ne vidi Intel GPU na $INTEL_RENDER."; fi
  for codec in h264_vaapi hevc_vaapi; do
    if runuser -u jellyfin -- "$FF" -hide_banner -v error -vaapi_device "$INTEL_RENDER" -f lavfi -i testsrc2=size=1920x1080:rate=30 -t 3 \
         -vf 'format=nv12,hwupload' -c:v "$codec" -f null - ; then
      okk "Test $codec: radi"
    else wrn "Test $codec nije uspio – provjeri prosljeđivanje $INTEL_RENDER."; fi
  done
fi
for d in "${M_LXC[@]}"; do
  if runuser -u jellyfin -- ls "$d" >/dev/null 2>&1; then okk "Mediji $d: dostupni ($(ls -1 "$d" 2>/dev/null | wc -l) stavki)"
  else wrn "Korisnik jellyfin ne može čitati $d (NFS export / UID/GID)."; fi
done
if (( HOLD )); then
  apt-mark hold jellyfin-server jellyfin-web >/dev/null
  okk "Jellyfin zaključan na $SRC_VERSION (nadogradnja: apt-mark unhold jellyfin-server jellyfin-web)."
fi
rm -f /root/jf-migrate.env /root/jf-inner.sh
INNER
}

inner_reader() {   # prevodi @@ poruke iz LXC-a u UI, ostalo u log
  local line
  while IFS= read -r line; do
    case $line in
      "@@STEP "*) msg_info "${line#@@STEP }" ;;
      "@@OK "*)   ok "${line#@@OK }" ;;
      "@@WARN "*) warn "${line#@@WARN }" ;;
      "@@FAIL "*) stop_spin; echo " ${R}✖ ${line#@@FAIL }${N}" >&4; log "FAIL(LXC) ${line#@@FAIL }" ;;
      *) log "LXC  $line" ;;
    esac
  done
  stop_spin
}

phase_inside() {
  hdr "Korak 4/4 · Jellyfin u LXC-u $CT_ID"
  pct status "$CT_ID" | grep -q running || run pct start "$CT_ID"
  local DEC_CODECS rc=0
  NV_RUN=""
  if [[ $GPU_MODE == nvidia ]]; then
    NV_VERSION=$(host_driver_version)
    [[ -n $NV_VERSION ]] || die "Ne mogu odrediti verziju NVIDIA drajvera na hostu."
    dl_nvidia_run "$NV_VERSION"
  fi
  DEC_CODECS=$(gpu_codecs)
  local NFS_LINES=() i opts
  if (( ! CT_UNPRIV )); then
    for i in "${!M_NFS[@]}"; do
      opts="rw,hard,noatime,_netdev,nofail"; (( MEDIA_RO )) && opts="ro,hard,noatime,_netdev,nofail"
      NFS_LINES+=("${M_NFS[$i]}|${M_LXC[$i]}|$opts")
    done
  fi
  declare -p SRC_VERSION JF_VERSION_MODE NV_VERSION MEDIA_UID MEDIA_GID APPLY_HWACCEL CACHE_INNER \
      NFS_LINES M_LXC GPU_MODE INTEL_RENDER DEC_CODECS NEW_SERVER_ID JF_SERVER_NAME > "$STATE_DIR/jf-migrate.env"
  write_inner_script

  msg_info "Kopiram skriptu i instalacijske datoteke u LXC…"
  run pct push "$CT_ID" "$STATE_DIR/jf-migrate.env" /root/jf-migrate.env
  run pct push "$CT_ID" "$STATE_DIR/jf-inner.sh"    /root/jf-inner.sh
  [[ -n $NV_RUN ]] && run pct push "$CT_ID" "$NV_RUN" /root/NVIDIA.run
  ok "Datoteke kopirane u LXC."

  run pct exec "$CT_ID" -- bash -c 'rm -rf /root/jfsrc && mkdir -p /root/jfsrc'
  ( trap - ERR; set +e; pct exec "$CT_ID" -- tar -xzpf - -C /root/jfsrc < "$TARBALL" >>"$LOG" 2>&1 ) &
  spin_wait $! "Raspakiravam Jellyfin podatke u LXC ($(du -h "$TARBALL" | cut -f1))" || rc=$?
  if (( rc )); then
    msg_info "Streaming nije uspio – kopiram arhivu preko pct push…"
    run pct push "$CT_ID" "$TARBALL" /root/jf.tar.gz
    run pct exec "$CT_ID" -- bash -c 'tar -xzpf /root/jf.tar.gz -C /root/jfsrc && rm -f /root/jf.tar.gz'
  fi
  ok "Podaci raspakirani u LXC."

  if ! pct exec "$CT_ID" -- env LC_ALL=C.UTF-8 bash /root/jf-inner.sh 2>&1 | inner_reader; then
    stop_spin
    echo " ${R}✖ Instalacija unutar LXC-a $CT_ID nije uspjela – razlog je ispisan iznad.${N}" >&4
    echo "   Nakon ispravka nastavi s:  ${B}bash $SELF --resume${N}  (LXC i izvezeni podaci ostaju)" >&4
    log "FAIL inner script"
    exit 1
  fi
}

# ============================================================================= sinkronizacija drajvera
sync_nvidia() {
  local id=$1 hv
  hdr "Usklađivanje NVIDIA drajvera u LXC $id"
  hv=$(host_driver_version); [[ -n $hv ]] || die "NVIDIA drajver ne radi na hostu."
  dl_nvidia_run "$hv"
  run pct push "$id" "$NV_RUN" /root/NVIDIA.run
  ( trap - ERR; set +e
    pct exec "$id" -- bash -c 'systemctl stop jellyfin 2>/dev/null || true
      sh /root/NVIDIA.run --silent --no-kernel-module --no-nouveau-check --no-x-check \
        || sh /root/NVIDIA.run --silent --no-kernel-modules --no-nouveau-check --no-x-check
      rc=$?; rm -f /root/NVIDIA.run; nvidia-smi -L && systemctl start jellyfin; exit $rc' >>"$LOG" 2>&1 ) &
  spin_wait $! "Instaliram NVIDIA $hv u LXC $id" || die "Usklađivanje nije uspjelo."
  ok "LXC $id koristi NVIDIA $hv; Jellyfin ponovno pokrenut."
}

# ============================================================================= završetak
finish() {
  local ip
  ip=$(pct exec "$CT_ID" -- hostname -I 2>/dev/null | awk '{print $1}' || true)
  sed -i '/CT_PASSWORD/d' "$ANSWERS" 2>/dev/null || true
  rm -f "$DRAFT"   # migracija uspjela – zapamćeni odgovori više nisu potrebni
  rm -f "$STATE_DIR/jf-migrate.env"
  local next="
 • Nginx Proxy Manager: upstream za novi Jellyfin je ${ip:-<IP>}:8096
 • Seerr / Tracearr / ArrHub: dodaj ili prebaci Jellyfin URL (API ključevi ostaju isti)"
  if [[ $GPU_MODE == nvidia ]]; then next+="
 • Dashboard → Playback → Transcoding: provjeri NVIDIA NVENC
 • Tijekom transkodiranja na hostu:  nvidia-smi
 • Nakon nadogradnje NVIDIA drajvera na hostu:
     bash $SELF --sync-nvidia $CT_ID"
  else next+="
 • Dashboard → Playback → Transcoding: provjeri Intel QuickSync ($INTEL_RENDER)
 • Tijekom transkodiranja na hostu:  intel_gpu_top  (apt install intel-gpu-tools)"
  fi
  if (( DISABLE_OLD )); then next+="
 • Kad sve provjeriš, stari kontejner možeš obrisati:  docker rm $CONTAINER"
  else next+="
 • Oba servera čitaju iste medije: na jednom isključi spremanje NFO/slika
   u mape medija (Biblioteke → Metadata savers)."
  fi
  wt_msg "Migracija završena" "Jellyfin radi na:   http://${ip:-<IP-LXC-a>}:8096

LXC $CT_ID '$CT_HOSTNAME' · disk na $CT_STORAGE
Stari kontejner: $( ((DISABLE_OLD)) && echo 'zaustavljen (nije obrisan)' || echo 'radi paralelno' )
Backup izvoza: $TARBALL

SLJEDEĆE:$next"
  hdr "Migracija završena"
  echo " ${G}✔${N} Jellyfin:  ${B}http://${ip:-<IP-LXC-a>}:8096${N}   (LXC $CT_ID '$CT_HOSTNAME')" >&4
  echo " ${G}✔${N} Backup:    $TARBALL" >&4
  echo " ${G}✔${N} Log:       $LOG" >&4
  printf '%s\n' "$next" | sed 's/^/ /' >&4
  echo >&4
}

usage() { sed -n '3,21p' "$SELF" | sed 's/^# \{0,1\}//'; }

main() {
  local args=() a
  for a in "$@"; do [[ $a == --text || $a == --api ]] || args+=("$a"); done
  local arg1=${args[0]:-}
  case $arg1 in
    -h|--help) usage; exit 0 ;;
    --sync-nvidia)
      [[ ${args[1]:-} =~ ^[0-9]+$ ]] || die "Upotreba: bash $SELF --sync-nvidia <ID-LXC-a>"
      preflight "$@"; sync_nvidia "${args[1]}"; exit 0 ;;
  esac
  log "===== jellyfin-docker2lxc v$SCRIPT_VERSION start ($*)"
  preflight "$@"
  if [[ -f $ANSWERS ]] && { [[ $arg1 == --resume ]] || ask_yn "Pronađena je nedovršena migracija.

Nastaviti gdje je stala?" y; }; then
    # shellcheck disable=SC1090
    source "$ANSWERS"
    banner; ok "Učitane spremljene postavke."
    if [[ -z ${CT_PASSWORD:-} && ! -f $STATE_DIR/.done_create ]]; then ask_secret CT_PASSWORD "Root lozinka za novi LXC:"; fi
  else
    [[ $arg1 == --resume ]] && die "Nema spremljene migracije za nastavak."
    rm -f "$STATE_DIR"/.done_* "$ANSWERS"
    DRAFT_MODE=""
    if [[ -s $DRAFT ]]; then
      # shellcheck disable=SC1090
      source "$DRAFT" 2>>"$LOG" || DRAFT_VAL=()
      if ((${#DRAFT_VAL[@]})); then
        local dc when
        when=$(date -r "$DRAFT" '+%d.%m. u %H:%M')
        CUR_SECTION="Zapamćeni odgovori"
        ask_menu dc "Pronađeni su odgovori iz prošlog pokretanja (${#DRAFT_VAL[@]} odgovora, spremljeno $when).

Kako nastaviti?" skip \
          skip    "Preskoči već odgovorena pitanja (preporučeno)" \
          default "Prođi sva pitanja – prošli odgovori su ponuđeni" \
          new     "Kreni ispočetka – zaboravi prošle odgovore"
        case $dc in
          skip|default) DRAFT_MODE=$dc ;;
          *) DRAFT_VAL=(); unset DRAFT_M_SRC DRAFT_M_DEST DRAFT_M_NFS DRAFT_M_LXC; rm -f "$DRAFT" ;;
        esac
      fi
    fi
    SUMMARY_OK=0
    while :; do collect_answers; (( SUMMARY_OK )) && break; DRAFT_MODE=default; done
    save_answers
    banner
  fi
  run_phase gpu    phase_gpu_host
  run_phase export phase_export
  run_phase create phase_create_lxc
  run_phase inside phase_inside
  finish
}

main "$@"
