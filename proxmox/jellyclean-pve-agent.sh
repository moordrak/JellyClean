#!/bin/bash
# =============================================================================
#  jellyclean-pve-agent – JEDINO što JellyCleanov SSH ključ smije pokrenuti
#  na Proxmox hostu.
#
#  /root/.ssh/authorized_keys (na Proxmoxu: /etc/pve/priv/authorized_keys):
#    from="<IP JellyCleana>",command="/usr/local/sbin/jellyclean-pve-agent <INST>:<KEYID>",restrict ssh-ed25519 AAAA… jellyclean-pve <INST>:<KEYID>
#
#  sshd ignorira naredbu koju klijent pošalje i uvijek pokreće ovaj program;
#  poslana naredba stiže u $SSH_ORIGINAL_COMMAND i dopuštene su samo ove radnje:
#    ping                 verzija agenta i migracijske skripte, ID ključa
#    start                pokreni migraciju (jellyfin-docker2lxc --api) kao systemd jedinicu
#    status <pomak>       stanje, novi redci napretka od bajta <pomak>, trenutno pitanje
#    answer <id>          odgovor na pitanje <id> (JSON na stdin, najviše 64 KB)
#    cancel               prekini migraciju
#    log                  zadnjih 300 redaka loga migracije
#    revoke               ukloni OVAJ ključ iz authorized_keys (Ukloni pristup)
#  'restrict' isključuje shell (pty), prosljeđivanje portova, agenta i X11.
# =============================================================================
set -uo pipefail
AGENT_VERSION=1
KEY=${1:-}
MIGRATE=/usr/local/sbin/jellyfin-docker2lxc
UNIT=jellyclean-migrate
STATE_DIR=/root/jellyfin-migration
API_DIR=$STATE_DIR/api
export LC_ALL=C.UTF-8 PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

fail() { jq -cn --arg e "$1" '{ok:false,error:$e}'; exit 1; }
command -v jq >/dev/null || { echo '{"ok":false,"error":"jq nije instaliran na Proxmoxu"}'; exit 1; }
[[ $KEY =~ ^[A-Za-z0-9]+:[A-Za-z0-9]+$ ]] || fail "agent je pokrenut bez ispravnog ID-a ključa"

CMD=${SSH_ORIGINAL_COMMAND:-}
[[ ${#CMD} -le 200 && $CMD =~ ^[A-Za-z0-9\ ._-]*$ ]] || fail "neispravna naredba"
read -r -a ARGV <<<"$CMD"
ACT=${ARGV[0]:-ping}

migrate_version() { grep -m1 -oP '^SCRIPT_VERSION="\K[^"]+' "$MIGRATE" 2>/dev/null || true; }
unit_active() { systemctl is-active -q "$UNIT" 2>/dev/null; }
authkeys() { readlink -f /root/.ssh/authorized_keys; }

case $ACT in
  ping|version)
    jq -cn --arg key "$KEY" --argjson av "$AGENT_VERSION" --arg mv "$(migrate_version)" \
           --arg host "$(hostname)" --arg pve "$(pveversion 2>/dev/null | head -n1)" \
           --arg from "${SSH_CLIENT%% *}" --argjson running "$(unit_active && echo true || echo false)" \
      '{ok:true,key:$key,agent:$av,migrate:$mv,host:$host,pve:$pve,from:$from,running:$running}' ;;

  start)
    [[ -x $MIGRATE ]] || fail "migracijska skripta nije instalirana ($MIGRATE)"
    unit_active && fail "migracija već radi"
    systemctl reset-failed "$UNIT" >/dev/null 2>&1 || true
    mkdir -p "$API_DIR" && chmod 700 "$STATE_DIR" "$API_DIR"
    rm -f "$API_DIR/progress.log" "$API_DIR/question.json" "$API_DIR/answer.json" "$API_DIR/exit"
    : > "$API_DIR/progress.log"
    if systemd-run --unit="$UNIT" --collect --quiet --setenv=TERM=dumb --setenv=LANG=C.UTF-8 \
         -p KillMode=mixed "$MIGRATE" --api >/dev/null 2>&1; then
      jq -cn '{ok:true}'
    else
      fail "systemd-run nije uspio pokrenuti migraciju"
    fi ;;

  status)
    off=${ARGV[1]:-0}; [[ $off =~ ^[0-9]+$ ]] || off=0
    P=$API_DIR/progress.log; size=0; [[ -f $P ]] && size=$(stat -c %s "$P")
    (( off > size )) && off=0
    if unit_active; then st=running; [[ -f $API_DIR/question.json ]] && st=waiting
    elif [[ -f $API_DIR/exit ]]; then
      if [[ $(cat "$API_DIR/exit") == 0 ]]; then st=done; else st=failed; fi
    else st=idle; fi
    q=null; [[ $st == waiting ]] && q=$(jq -c . "$API_DIR/question.json" 2>/dev/null || echo null)
    { if [[ -f $P ]] && (( size > off )); then tail -c +"$((off + 1))" "$P" | head -c "$((size - off))"; fi; } \
      | jq -Rcs --arg st "$st" --argjson off "$size" --argjson q "$q" \
          --arg exit "$(cat "$API_DIR/exit" 2>/dev/null || true)" \
          '{ok:true,state:$st,offset:$off,text:.,question:$q,exit:$exit}' ;;

  answer)
    id=${ARGV[1]:-}; [[ $id =~ ^[0-9]+$ ]] || fail "neispravan ID pitanja"
    [[ -f $API_DIR/question.json ]] || fail "nema otvorenog pitanja"
    [[ $(jq -r .id "$API_DIR/question.json" 2>/dev/null) == "$id" ]] || fail "pitanje #$id više nije aktualno"
    body=$(head -c 65536)
    jq -e --argjson id "$id" 'type=="object" and .id==$id' <<<"$body" >/dev/null 2>&1 || fail "neispravan odgovor"
    ( umask 077; jq -c '{id:.id,value:(.value // ""),cancel:(.cancel // false)}' <<<"$body" > "$API_DIR/answer.tmp" ) \
      && mv -f "$API_DIR/answer.tmp" "$API_DIR/answer.json" && jq -cn '{ok:true}' ;;

  cancel)
    if [[ -f $API_DIR/question.json ]]; then
      id=$(jq -r .id "$API_DIR/question.json" 2>/dev/null)
      ( umask 077; jq -cn --argjson id "${id:-0}" '{id:$id,cancel:true}' > "$API_DIR/answer.json" )
      sleep 2
    fi
    unit_active && systemctl stop "$UNIT" >/dev/null 2>&1
    [[ -f $API_DIR/exit ]] || echo 130 > "$API_DIR/exit"
    jq -cn '{ok:true}' ;;

  log)
    tail -n 300 "$STATE_DIR/migration.log" 2>/dev/null | jq -Rcs '{ok:true,text:.}' ;;

  revoke)
    AK=$(authkeys); [[ -f $AK ]] || fail "authorized_keys ne postoji"
    T=$(mktemp) || fail "mktemp"
    grep -vF " jellyclean-pve $KEY" "$AK" > "$T" || true
    cat "$T" > "$AK"; rm -f "$T"   # pisanje u postojeću datoteku čuva poveznicu na /etc/pve/priv
    jq -cn --arg key "$KEY" '{ok:true,revoked:$key}' ;;

  *) fail "nepoznata radnja: $ACT" ;;
esac
