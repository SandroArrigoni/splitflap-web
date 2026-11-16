#!/usr/bin/env bash
# Pull the latest version and restart the service.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
git pull --ff-only
sudo systemctl restart splitflap-web
echo "updated to $(git log -1 --format='%h %s')"
