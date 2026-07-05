#!/usr/bin/env bash
# Remove Sasha's services for one user. Keeps config, auth, and usage data
# (delete ~USER/.config/sasha and ~USER/.local/state/sasha yourself if wanted).
#   sudo ./uninstall.sh --user sheridanh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "must run as root"; exit 1; }
USER_NAME=""
[[ "${1:-}" == "--user" && -n "${2:-}" ]] && USER_NAME="$2"
[[ -n "$USER_NAME" ]] || { echo "usage: sudo $0 --user NAME"; exit 1; }

systemctl disable --now "sasha-web-$USER_NAME" "sasha-term-$USER_NAME" 2>/dev/null || true
rm -f "/etc/systemd/system/sasha-web-$USER_NAME.service" \
      "/etc/systemd/system/sasha-term-$USER_NAME.service"
systemctl daemon-reload
su - "$USER_NAME" -c "tmux kill-session -t sasha-$USER_NAME" 2>/dev/null || true
echo "removed services for $USER_NAME (code in /usr/local/lib/sasha/ left for other users)"
