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

TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT
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
  pct exec "$id" -- env GH_TOKEN="${GH_TOKEN:-}" /usr/local/bin/jellyclean-update || echo "✘ nadogradnja kontejnera $id nije uspjela"
done
