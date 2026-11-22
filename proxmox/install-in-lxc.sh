#!/usr/bin/env bash
# JellyClean - instalacija u postojeći Debian/Ubuntu LXC (pokreni kao root unutar LXC-a)
# Preduvjet: mediji su već montirani u LXC (NFS ili Proxmox bind mount, npr. mp0: /mnt/nas,mp=/mnt/media)
set -euo pipefail
REPO="${REPO:-https://github.com/moordrak/JellyClean.git}"
DIR=/opt/jellyclean
RUN_USER="${RUN_USER:-root}"

apt-get update -qq
apt-get install -y -qq python3 git >/dev/null

if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
elif [ -f "$(dirname "$0")/../app.py" ]; then
  mkdir -p "$DIR"; cp -r "$(dirname "$0")"/../{app.py,static,.env.example} "$DIR/"
else
  git clone --depth 1 "$REPO" "$DIR"
fi

if [ ! -f /etc/jellyclean.env ]; then
  cp "$DIR/.env.example" /etc/jellyclean.env 2>/dev/null || cat > /etc/jellyclean.env <<'EOT'
PORT=8095
MEDIA_PATHS=Filmovi=/mnt/media/movies,Serije=/mnt/media/tv
JELLYFIN_URL=
JELLYFIN_API_KEY=
AUTH_USER=admin
AUTH_PASS=
EOT
  chmod 600 /etc/jellyclean.env
  echo ">> Uredi /etc/jellyclean.env pa pokreni: systemctl restart jellyclean"
fi

cat > /etc/systemd/system/jellyclean.service <<EOT
[Unit]
Description=JellyClean
After=network-online.target remote-fs.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/jellyclean.env
Environment=DATA_DIR=/var/lib/jellyclean
User=${RUN_USER}
ExecStart=/usr/bin/python3 ${DIR}/app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOT
mkdir -p /var/lib/jellyclean
systemctl daemon-reload
systemctl enable --now jellyclean
IP=$(hostname -I | awk '{print $1}')
echo ">> JellyClean radi na http://${IP}:$(grep -oP '^PORT=\K.*' /etc/jellyclean.env || echo 8095)"
