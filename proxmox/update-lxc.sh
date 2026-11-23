#!/usr/bin/env bash
# ============================================================================
#  JellyClean - nadogradnja postojećeg LXC kontejnera (pokreni na Proxmox HOSTU)
#     bash -c "$(curl -fsSL https://raw.githubusercontent.com/moordrak/JellyClean/main/proxmox/update-lxc.sh)"
#     bash proxmox/update-lxc.sh [ID ...]          # bez ID-a sam traži JellyClean kontejnere
#     GH_TOKEN=xxx bash proxmox/update-lxc.sh      # privatni repozitorij
#  Postavi (ili osvježi) /usr/local/bin/jellyclean-update u kontejneru i pokrene ga.
# ============================================================================
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Pokreni kao root na Proxmox hostu."; exit 1; }
command -v pct >/dev/null || { echo "Ovo nije Proxmox VE host."; exit 1; }

IDS=("$@")
if (( ${#IDS[@]} == 0 )); then
  for c in $(pct list | awk 'NR>1{print $1}'); do
    if pct config "$c" 2>/dev/null | grep -qiE '^(tags:.*jellyclean|hostname: *jellyclean)'; then IDS+=("$c"); fi
  done
fi
(( ${#IDS[@]} )) || { echo "Nije pronađen JellyClean kontejner. Upotreba: $0 <ID>"; exit 1; }

TMP=$(mktemp); T=$(mktemp -d); trap 'rm -rf "$TMP" "$T"' EXIT

# Ako JellyClean na Jellyfin računalu koristi ograničeni pomoćni program (jellyclean-helper),
# provjeri mu verziju i po potrebi ga osvježi (traži sudo lozinku SSH korisnika, ne sprema se).
refresh_helper() {
  local id=$1 envf H HOST SUSER PORT KEY OPTS want have PW out
  envf=$(pct exec "$id" -- cat /etc/jellyclean.env 2>/dev/null) || return 0
  get() { echo "$envf" | sed -n "s/^$1=//p" | head -1 | sed 's/^"//;s/"$//'; }
  H=$(get JF_SSH_HELPER); [[ -n "$H" ]] || return 0
  HOST=$(get JF_SSH_HOST); SUSER=$(get JF_SSH_USER); PORT=$(get JF_SSH_PORT); KEY=$(get JF_SSH_KEY)
  OPTS="-p ${PORT:-22} -i ${KEY:-/var/lib/jellyclean/ssh/id_ed25519} -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/var/lib/jellyclean/ssh/known_hosts -o ConnectTimeout=10 -o LogLevel=ERROR"
  want=$(pct exec "$id" -- bash -c 'cd /opt/jellyclean && python3 -c "import app; print(app.HELPER_VERSION)"' 2>/dev/null | tr -d '\r' || true)
  [[ -n "$want" ]] || return 0
  have=$(pct exec "$id" -- bash -c "ssh $OPTS '$SUSER@$HOST' 'sudo -n $H version' 2>/dev/null" | sed -n 's/^HVER //p' | tr -d '\r' || true)
  if [[ "$have" == "$want" ]]; then echo "✔ pomoćni program na $HOST je ažuran (v$have)"; return 0; fi
  echo "➜ pomoćni program na $HOST je zastario (v${have:-1} → v$want) i treba ga osvježiti."
  pct exec "$id" -- bash -c 'set -a; . /etc/jellyclean.env; set +a; cd /opt/jellyclean && python3 app.py --helper-script' > "$T/helper.sh" 2>/dev/null || true
  grep -q '^main "\$@"' "$T/helper.sh" || { echo "✘ generiranje pomoćnog programa nije uspjelo"; return 0; }
  for _ in 1 2 3; do
    printf 'Sudo lozinka za %s@%s (Enter = preskoči): ' "$SUSER" "$HOST"; IFS= read -r -s PW </dev/tty || PW=""; echo
    [[ -n "$PW" ]] || { echo "  preskočeno - JellyClean će prikazivati upozorenje dok se ne osvježi"; return 0; }
    { printf '%s\n' "$PW"; echo 'set -e'; echo "cat > $H.new <<'JC_HELPER_EOF'"; cat "$T/helper.sh"; echo 'JC_HELPER_EOF'
      echo "chown root:root $H.new && chmod 755 $H.new && mv -f $H.new $H && echo HELPER_OK"; } > "$T/payload"
    PW=""; chmod 600 "$T/payload"
    pct push "$id" "$T/payload" /root/jc-payload --perms 600; rm -f "$T/payload"
    out=$(pct exec "$id" -- bash -c "ssh $OPTS '$SUSER@$HOST' \"sudo -k -S -p '' sh -s\" < /root/jc-payload 2>/dev/null; rm -f /root/jc-payload" || true)
    if [[ "$out" == *HELPER_OK* ]]; then echo "✔ pomoćni program osvježen (v$want)"; return 0; fi
    echo "  ✘ nije uspjelo (kriva lozinka?)"
  done
}
cat > "$TMP" <<'UPDEOF'
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

for id in "${IDS[@]}"; do
  echo "=== kontejner $id ($(pct config "$id" | sed -n 's/^hostname: //p'))"
  if [[ "$(pct status "$id" | awk '{print $2}')" != running ]]; then echo "➜ pokrećem…"; pct start "$id"; sleep 5; fi
  if ! pct exec "$id" -- test -f /opt/jellyclean/app.py; then echo "✘ u kontejneru nema /opt/jellyclean/app.py - preskačem"; continue; fi
  pct exec "$id" -- bash -c 'command -v curl >/dev/null || (apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null)'
  pct push "$id" "$TMP" /usr/local/bin/jellyclean-update --perms 755
  if pct exec "$id" -- env GH_TOKEN="${GH_TOKEN:-}" /usr/local/bin/jellyclean-update; then
    refresh_helper "$id"
  else
    echo "✘ nadogradnja kontejnera $id nije uspjela"
  fi
done
