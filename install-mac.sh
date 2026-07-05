#!/usr/bin/env bash
# Sasha on hermes — macOS SERVICE install (launchd, no root, no systemd).
# Two per-user LaunchAgents that auto-start on login and auto-restart on crash:
#   com.sasha.gw   — the hermes gateway (loopback ws) via sasha-gw
#   com.sasha.web  — the dashboard (native chat), loopback
# Survives logout/reboot; stops the Mac from needing a terminal held open.
#
#   ./install-mac.sh [--name Craig] [--place "Home"] [--port 7790] [--gw-port 7792]
#   ./install-mac.sh --uninstall
#
# Prereqs: hermes on PATH, hermes web UI built once (run ./run-mac.sh first —
# it builds it and proves the config; this promotes that to a service).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LA="$HOME/Library/LaunchAgents"
LIB="$HOME/.local/lib/sasha"
CFG_DIR="$HOME/.config/sasha"
GUI="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
  for L in com.sasha.web com.sasha.gw; do
    launchctl bootout "$GUI/$L" 2>/dev/null || launchctl unload "$LA/$L.plist" 2>/dev/null || true
    rm -f "$LA/$L.plist"
  done
  echo "Sasha services removed (config, auth, and me/ kept)."
  exit 0
fi

NAME="${USER}" PLACE="Home" PORT=7790 GW_PORT=7792
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2;;
    --place) PLACE="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --gw-port) GW_PORT="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done

command -v python3 >/dev/null || { echo "need python3"; exit 1; }
HERMES_BIN="$(command -v hermes || true)"
[[ -n "$HERMES_BIN" ]] || { echo "hermes not on PATH — run ./run-mac.sh first (it sets this up)"; exit 1; }
PY3="$(command -v python3)"
# launchd gives a minimal PATH; give the agents the dirs their commands live in.
AGENT_PATH="$(dirname "$HERMES_BIN"):$(dirname "$PY3"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"

echo "==> Installing package code to $LIB"
mkdir -p "$LIB"
install -m 0644 "$HERE/dashboard.py"  "$LIB/dashboard.py"
install -m 0755 "$HERE/sasha-gw"      "$LIB/sasha-gw"
install -m 0755 "$HERE/me_bridge.py"  "$LIB/me_bridge.py"
install -m 0644 "$HERE/capabilities.py" "$LIB/capabilities.py"

echo "==> Config + auth"
mkdir -p "$CFG_DIR"
if [[ ! -f "$CFG_DIR/config.json" ]]; then
  cat > "$CFG_DIR/config.json" <<EOF
{
  "name": "$NAME",
  "place": "$PLACE",
  "port": $PORT,
  "gw_port": $GW_PORT,
  "chat_mode": "ws",
  "host": "127.0.0.1",
  "auth_file": "$CFG_DIR/auth",
  "me_dir": "~/ai-os/me",
  "telemetry": true
}
EOF
fi
if [[ ! -f "$CFG_DIR/auth" ]]; then
  PW=$(head -c18 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c14)
  LOGIN=$(printf '%s' "$NAME" | tr '[:upper:]' '[:lower:]')
  printf '%s:%s' "$LOGIN" "$PW" > "$CFG_DIR/auth"; chmod 0600 "$CFG_DIR/auth"
  echo "    sign-in (save it now): $LOGIN:$PW"
fi

echo "==> me/ passport bridge"
python3 "$LIB/me_bridge.py"

echo "==> Writing LaunchAgents"
mkdir -p "$LA"
write_plist() {  # $1=label  $2=program-args-xml  $3=extra-env-xml
  cat > "$LA/$1.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key><array>$2</array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$AGENT_PATH</string>
$3  </dict>
  <key>WorkingDirectory</key><string>$HOME</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$CFG_DIR/$1.log</string>
  <key>StandardErrorPath</key><string>$CFG_DIR/$1.log</string>
</dict></plist>
EOF
}

write_plist "com.sasha.gw" \
  "<string>/bin/bash</string><string>-c</string><string>exec $LIB/sasha-gw $GW_PORT</string>" \
  "    <key>HERMES_DASHBOARD_TUI</key><string>1</string>
"
write_plist "com.sasha.web" \
  "<string>$PY3</string><string>$LIB/dashboard.py</string><string>--host</string><string>127.0.0.1</string><string>--port</string><string>$PORT</string>" \
  "    <key>SASHA_CONFIG</key><string>$CFG_DIR/config.json</string>
"

echo "==> Loading services"
for L in com.sasha.gw com.sasha.web; do
  launchctl bootout "$GUI/$L" 2>/dev/null || true
  launchctl bootstrap "$GUI" "$LA/$L.plist" 2>/dev/null \
    || launchctl load -w "$LA/$L.plist"
done
sleep 6

echo "==> Verify"
for L in com.sasha.gw com.sasha.web; do
  if launchctl print "$GUI/$L" >/dev/null 2>&1; then echo "  $L: loaded"; else echo "  $L: NOT loaded — see $CFG_DIR/$L.log"; fi
done
curl -s -m4 -o /dev/null -w "  gateway 127.0.0.1:$GW_PORT: %{http_code}\n" "http://127.0.0.1:$GW_PORT/" || true
curl -s -m4 -o /dev/null -w "  page 127.0.0.1:$PORT: %{http_code} (401 = up, needs sign-in)\n" "http://127.0.0.1:$PORT/" || true
echo
echo "==> done — Sasha runs on login now.  http://127.0.0.1:$PORT/"
echo "    logs: $CFG_DIR/com.sasha.{gw,web}.log     stop: ./install-mac.sh --uninstall"
echo "    NOTE: a laptop still sleeps — the page pauses when the Mac is asleep and"
echo "    resumes on wake. For always-on, run it on a machine that stays awake."
