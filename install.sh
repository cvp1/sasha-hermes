#!/usr/bin/env bash
# Sasha installer — stand up the accessibility layer on top of an existing
# hermes install, for one user.
#
#   sudo ./install.sh --user sheridanh --name Sheridan [--place "The ranch"]
#                     [--port 7790] [--term-port 7791] [--force-config]
#
# What it does:
#   * installs dashboard.py + the chat-pane wrapper to /usr/local/lib/sasha/
#   * writes ~USER/.config/sasha/config.json (kept if it exists) + a generated
#     Basic-Auth credential (printed ONCE at the end)
#   * installs two systemd units:
#       sasha-term-USER  — ttyd on LOOPBACK ONLY (canvas renderer, warm-paper
#                          xterm theme) wrapping `hermes` in tmux
#       sasha-web-USER   — the dashboard, which reverse-proxies the chat pane
#                          behind its Basic Auth
#   * verifies: units active, term port NOT reachable from the LAN, web 401s
#     without credentials.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "must run as root (sudo $0 ...)"; exit 1; }

HERE="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="" DISPLAY_NAME="" PLACE="Home" PORT=7790 TERM_PORT=7791 GW_PORT=7792 FORCE_CONFIG=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) USER_NAME="$2"; shift 2;;
    --name) DISPLAY_NAME="$2"; shift 2;;
    --place) PLACE="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --term-port) TERM_PORT="$2"; shift 2;;
    --gw-port) GW_PORT="$2"; shift 2;;
    --force-config) FORCE_CONFIG=1; shift;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done
[[ -n "$USER_NAME" && -n "$DISPLAY_NAME" ]] || { echo "need --user and --name"; exit 1; }
id "$USER_NAME" >/dev/null || exit 1
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)

echo "==> Checking dependencies"
for dep in ttyd tmux python3; do
  command -v "$dep" >/dev/null || { echo "missing dependency: $dep"; exit 1; }
done
su - "$USER_NAME" -c 'command -v hermes' >/dev/null \
  || { echo "hermes not on $USER_NAME's login PATH — install hermes first"; exit 1; }
echo "    ok (ttyd, tmux, python3, hermes)"

echo "==> Installing code to /usr/local/lib/sasha/"
install -d /usr/local/lib/sasha
install -m 0644 "$HERE/dashboard.py" /usr/local/lib/sasha/dashboard.py
install -m 0755 "$HERE/sasha-term"  /usr/local/lib/sasha/sasha-term
install -m 0755 "$HERE/sasha-gw"    /usr/local/lib/sasha/sasha-gw
install -m 0755 "$HERE/me_bridge.py" /usr/local/lib/sasha/me_bridge.py
install -m 0644 "$HERE/capabilities.py" /usr/local/lib/sasha/capabilities.py

echo "==> Config + auth for $USER_NAME"
CFG_DIR="$USER_HOME/.config/sasha"
install -d -o "$USER_NAME" -g "$USER_NAME" "$CFG_DIR"
if [[ ! -f "$CFG_DIR/config.json" || $FORCE_CONFIG -eq 1 ]]; then
  cat > "$CFG_DIR/config.json" <<EOF
{
  "name": "$DISPLAY_NAME",
  "place": "$PLACE",
  "port": $PORT,
  "term_port": $TERM_PORT,
  "gw_port": $GW_PORT,
  "chat_mode": "ws",
  "auth_file": "$CFG_DIR/auth",
  "telemetry": true
}
EOF
  chown "$USER_NAME:$USER_NAME" "$CFG_DIR/config.json"
  echo "    wrote config.json (edit it to add actions/search/services)"
else
  echo "    keeping existing config.json"
fi
if [[ ! -f "$CFG_DIR/auth" ]]; then
  # lowercase the USERNAME only — never the password (would halve its entropy)
  LOGIN=$(printf '%s' "$DISPLAY_NAME" | tr '[:upper:]' '[:lower:]')
  PW=$(head -c18 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c14)
  printf '%s:%s' "$LOGIN" "$PW" > "$CFG_DIR/auth"
  chown "$USER_NAME:$USER_NAME" "$CFG_DIR/auth"; chmod 0600 "$CFG_DIR/auth"
  NEW_CRED=$(cat "$CFG_DIR/auth")
else
  NEW_CRED=""
  echo "    keeping existing auth"
fi

echo "==> me/ passport bridge (shared identity with Sasha on Claude Code)"
su - "$USER_NAME" -c "python3 /usr/local/lib/sasha/me_bridge.py"

echo "==> Systemd units"
THEME='theme={"background":"#F1EADA","foreground":"#3A2F23","cursor":"#B4552D","cursorAccent":"#F1EADA","selectionBackground":"#E3D5B8","black":"#5A5044","red":"#B3392E","green":"#4F7A3F","yellow":"#9C6D1E","blue":"#3E6C8E","magenta":"#8E5A7C","cyan":"#40767C","white":"#EFE6D2","brightBlack":"#8A7A64","brightRed":"#C4483C","brightGreen":"#5E8F4C","brightYellow":"#B37F24","brightBlue":"#4E7DA3","brightMagenta":"#A06B8E","brightCyan":"#4E8A91","brightWhite":"#FDFAF4"}'
cat > "/etc/systemd/system/sasha-term-$USER_NAME.service" <<EOF
[Unit]
Description=Sasha chat pane (ttyd, loopback only) for $USER_NAME
After=network.target

[Service]
User=$USER_NAME
ExecStart=/usr/bin/ttyd -i 127.0.0.1 --port $TERM_PORT --writable -t rendererType canvas -t '$THEME' -b /term/hermes -w $USER_HOME tmux new-session -A -s sasha-$USER_NAME /usr/local/lib/sasha/sasha-term
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
cat > "/etc/systemd/system/sasha-gw-$USER_NAME.service" <<EOF
[Unit]
Description=Sasha agent gateway (hermes serve, loopback only) for $USER_NAME
After=network.target

[Service]
User=$USER_NAME
WorkingDirectory=$USER_HOME
Environment=HERMES_DASHBOARD_TUI=1
ExecStart=/bin/bash -lc '/usr/local/lib/sasha/sasha-gw $GW_PORT'
Restart=always
RestartSec=5
StartLimitIntervalSec=120
StartLimitBurst=10

[Install]
WantedBy=multi-user.target
EOF
cat > "/etc/systemd/system/sasha-web-$USER_NAME.service" <<EOF
[Unit]
Description=Sasha dashboard for $USER_NAME
After=network.target sasha-term-$USER_NAME.service

[Service]
User=$USER_NAME
Environment=SASHA_CONFIG=$CFG_DIR/config.json
ExecStart=/usr/bin/python3 /usr/local/lib/sasha/dashboard.py --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now "sasha-gw-$USER_NAME" "sasha-term-$USER_NAME" "sasha-web-$USER_NAME"
sleep 3

echo "==> Verify"
systemctl is-active "sasha-gw-$USER_NAME" "sasha-term-$USER_NAME" "sasha-web-$USER_NAME"
LAN_IP=$(hostname -I | awk '{print $1}')
for P in $TERM_PORT $GW_PORT; do
  if curl -s -o /dev/null -m 3 "http://$LAN_IP:$P/"; then
    echo "  !! backend port $P REACHABLE ON LAN — should be loopback only"; exit 1
  else
    echo "  ok: port $P not on LAN (loopback only)"
  fi
done
CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:$PORT/")
[[ "$CODE" == "401" ]] && echo "  ok: dashboard requires sign-in" || echo "  ?? dashboard returned $CODE without auth"

echo
echo "==> done — Sasha is at  http://$LAN_IP:$PORT/"
if [[ -n "$NEW_CRED" ]]; then
  echo "    sign-in (save this now — shown once):  $NEW_CRED"
fi
echo "    config: $CFG_DIR/config.json  (actions, search, weather, services)"
