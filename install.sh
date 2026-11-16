#!/usr/bin/env bash
# Install the split-flap web app as a service on the Raspberry Pi.
#
#   ./install.sh                       install / reinstall
#   ./install.sh --hostname splitflap  also rename the Pi -> http://splitflap.local
#   ./install.sh --port 8080           force a port (default: 80 if free, else 8080)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
NEW_HOSTNAME=""
PORT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --hostname) NEW_HOSTNAME="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

echo "==> Installing from $DIR for user $RUN_USER"

# 1. config.json (kept out of git so 'git pull' never overwrites your settings)
if [ ! -f "$DIR/config.json" ]; then
  cp "$DIR/config.example.json" "$DIR/config.json"
  echo "    created config.json from config.example.json"
fi

# 2. I2C: kernel module + permissions
if command -v raspi-config >/dev/null 2>&1; then
  sudo raspi-config nonint do_i2c 0 || true   # 0 = enable
fi
sudo usermod -aG i2c "$RUN_USER" || true

# 3. Port: 80 gives the nicest URL, but the old PHP setup may still use it
# something other than this app (python3) listening on the port?
port_taken() { sudo ss -ltnpH "sport = :$1" 2>/dev/null | grep -v '"python3"' | grep -q . ; }
if [ -z "$PORT" ]; then
  if port_taken 80; then
    PORT=8080
    echo "    port 80 is taken (old Apache/lighttpd for the PHP UI?) -> using 8080"
    echo "    to get the plain URL later: stop that web server and run ./install.sh --port 80"
  else
    PORT=80
  fi
fi
python3 - "$DIR/config.json" "$PORT" <<'PY'
import json, sys, re
path, port = sys.argv[1], int(sys.argv[2])
text = open(path, encoding="utf-8").read()
text = re.sub(r'"port"\s*:\s*\d+', '"port": %d' % port, text, count=1)
json.loads(text)  # still valid?
open(path, "w", encoding="utf-8").write(text)
PY

# 4. mDNS so phones/laptops on the same Wi-Fi find <hostname>.local
if ! systemctl is-active --quiet avahi-daemon 2>/dev/null; then
  sudo apt-get install -y avahi-daemon && sudo systemctl enable --now avahi-daemon || \
    echo "    !! could not start avahi-daemon; use the Pi's IP address instead"
fi
if [ -n "$NEW_HOSTNAME" ] && [ "$NEW_HOSTNAME" != "$(hostname)" ]; then
  OLD="$(hostname)"
  sudo hostnamectl set-hostname "$NEW_HOSTNAME"
  sudo sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$NEW_HOSTNAME/" /etc/hosts
  sudo systemctl restart avahi-daemon || true
  echo "    renamed $OLD -> $NEW_HOSTNAME"
fi

# 5. systemd service
sed -e "s|@DIR@|$DIR|g" -e "s|@USER@|$RUN_USER|g" "$DIR/splitflap-web.service" \
  | sudo tee /etc/systemd/system/splitflap-web.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable splitflap-web >/dev/null
sudo systemctl restart splitflap-web

URL="http://$(hostname).local"
[ "$PORT" != "80" ] && URL="$URL:$PORT"
sleep 1
if systemctl is-active --quiet splitflap-web; then
  echo "==> Running. Open $URL on any device on the same Wi-Fi."
else
  echo "!! service did not start: sudo journalctl -u splitflap-web -n 50" >&2
  exit 1
fi
