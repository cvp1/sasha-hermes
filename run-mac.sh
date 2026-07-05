#!/usr/bin/env bash
# Sasha on hermes — macOS / laptop mode. No root, no systemd, no ttyd:
# native ws chat only. Starts the hermes gateway (background) + the dashboard
# (foreground); Ctrl-C stops both. First run creates your config + sign-in.
#
#   ./run-mac.sh [--name Craig] [--place "Home"] [--port 7790] [--gw-port 7792]
#
# Prereqs: python3, and `hermes` on PATH (pip install hermes-agent && hermes setup).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

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
if ! command -v hermes >/dev/null; then
  UB="$(python3 -m site --user-base 2>/dev/null)/bin"
  if [[ -x "$UB/hermes" ]]; then
    echo "found hermes at $UB (not on PATH) — using it for this run."
    echo "make it permanent:  echo 'export PATH=\"$UB:\$PATH\"' >> ~/.zshrc"
    export PATH="$UB:$PATH"
  else
    echo "need hermes:  pip install hermes-agent && hermes setup"
    echo "(command not found after installing? PATH fix in GRADUATION.md step 2)"
    exit 1
  fi
fi

CFG_DIR="$HOME/.config/sasha"
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
  echo "wrote $CFG_DIR/config.json (edit to add actions/search/services later)"
fi
if [[ ! -f "$CFG_DIR/auth" ]]; then
  PW=$(head -c18 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c14)
  LOGIN=$(printf '%s' "$NAME" | tr '[:upper:]' '[:lower:]')
  printf '%s:%s' "$LOGIN" "$PW" > "$CFG_DIR/auth"; chmod 0600 "$CFG_DIR/auth"
  echo "sign-in (save it now): $LOGIN:$PW"
fi

echo "==> me/ passport bridge (~/.hermes/SOUL.md -> ~/ai-os/me/)"
python3 "$HERE/me_bridge.py"

# Find the hermes web/ dir (needed to build the UI once). Official installer
# lays it under $HERMES_HOME/hermes-agent; git checkouts vary.
find_web_dir() {
  for d in "${HERMES_HOME:-$HOME/.hermes}/hermes-agent/web" \
           "$HOME/.hermes/hermes-agent/web"; do
    [[ -f "$d/package.json" ]] && { echo "$d"; return 0; }
  done
  # last resort: ask hermes' own python where the package lives
  local p; p="$(hermes --which-web 2>/dev/null || true)"
  [[ -n "$p" && -f "$p/package.json" ]] && { echo "$p"; return 0; }
  return 1
}

# Build the web UI once if it's missing (uses hermes' own node if present).
build_web_ui() {
  local w; w="$(find_web_dir)" || { echo "   couldn't find hermes' web/ dir — build manually then re-run."; return 1; }
  if ! command -v npm >/dev/null; then
    echo "   the web UI needs building once, but 'npm' isn't on your PATH."
    echo "   install Node (brew install node), then:  cd \"$w\" && npm install && npm run build"
    return 1
  fi
  echo "   building the hermes web UI once (a few minutes)…"
  ( cd "$w" && npm install && npm run build ) || { echo "   build failed — see output above."; return 1; }
  return 0
}

start_gateway() {   # $1 = serve|dashboard
  if [[ "$1" == "dashboard" ]]; then
    HERMES_DASHBOARD_TUI=1 hermes dashboard --host 127.0.0.1 --port "$GW_PORT" --skip-build --no-open \
      > "$CFG_DIR/gateway.log" 2>&1 &
  else
    HERMES_DASHBOARD_TUI=1 hermes serve --host 127.0.0.1 --port "$GW_PORT" --skip-build \
      > "$CFG_DIR/gateway.log" 2>&1 &
  fi
  GW_PID=$!
}

echo "==> starting hermes gateway on 127.0.0.1:$GW_PORT"
MODE=serve; BUILT=0
start_gateway "$MODE"
trap 'kill $GW_PID 2>/dev/null || true' EXIT

for i in $(seq 1 40); do
  curl -s -m 2 -o /dev/null "http://127.0.0.1:$GW_PORT/" && break
  if ! kill -0 $GW_PID 2>/dev/null; then
    if grep -q "invalid choice: 'serve'" "$CFG_DIR/gateway.log"; then
      echo "   this hermes has no 'serve' — using 'hermes dashboard'"
      MODE=dashboard; start_gateway "$MODE"
    elif grep -q "no web dist" "$CFG_DIR/gateway.log" && [[ "$BUILT" -eq 0 ]]; then
      BUILT=1
      build_web_ui || exit 1
      echo "   web UI built — restarting gateway"
      start_gateway "$MODE"
    else
      echo "   gateway failed — tail of $CFG_DIR/gateway.log:"; tail -6 "$CFG_DIR/gateway.log"; exit 1
    fi
  fi
  sleep 1
done
curl -s -m 2 -o /dev/null "http://127.0.0.1:$GW_PORT/" || { echo "gateway never came up — see $CFG_DIR/gateway.log"; exit 1; }
echo "   gateway up"

echo "==> Sasha at  http://127.0.0.1:$PORT/   (Ctrl-C stops everything)"
exec python3 "$HERE/dashboard.py" --host 127.0.0.1 --port "$PORT"
