# Sasha rollout notes — harvested root-causes (Story 021)

The Jul 4–5 2026 one-off rollout/fix scripts that lived in `_lib/` carried
real root-cause comments but were debris (untracked, single-use, several
sudo-deployed into sheridanh's tree). The scripts are deleted; their *why* is
preserved below — the header comment block of each, verbatim.

## apply_me_bridge.sh
```
Apply the me/ passport bridge LIVE for both users.

What it does (proven on a fresh gateway before staging — the agent quoted
the passport rule from standing context with no tools):
  * installs the updated dashboard.py (seeds ~/ai-os/me/ skeletons on boot,
    never overwrites) + me_bridge.py to /usr/local/lib/sasha
  * runs me_bridge.py AS EACH USER — injects an idempotent marked block into
    ~/.hermes/SOUL.md (the identity file hermes composes into every
    session's system prompt) pointing at ~/ai-os/me/
  * restarts both gateways (SOUL.md is read at gateway boot) + dashboards
cvande's SOUL.md is already injected + me/ seeded (done during validation);
re-running is a no-op by design.

Run with:  sudo bash /home/cvande/Github/CC/_lib/apply_me_bridge.sh
```

## apply_palette_a.sh
```
Apply palette A ("high-desert morning") to Sheridan's dashboard — Craig's pick
2026-07-04. RE-RUN SAFE; v2 fixes the unit patch (the theme anchored on
"-t rendererType canvas" but the unit reads "rendererType=canvas", so the
first run silently skipped the terminal theme — this run lands it, and also
carries the compact-header page update). Two halves:
  1. Page: token-level swap already in _lib/sasha_dashboard.py (deploys here).
  2. Chat pane: xterm theme on ttyd-sasha-hermes — warm paper background +
     a DARK warm ANSI ramp (light-theme yellows/greens are darkened so any
     REPL text stays legible on paper).
Craig's :8080 is untouched (operator baseline stays dark).

Run with:  sudo bash /home/cvande/Github/CC/_lib/apply_palette_a.sh
```

## deploy_int4_telemetry.sh
```
Deploy INT-4 usage telemetry + the redesigned Sasha UI (Craig :8080 + Sasha :8085).

1. Telemetry (Craig OK'd 2026-07-04, INT-4 / D51-A): aggregate EVENT COUNTS
   only — page loads, chip/run/search clicks, terminal mounts, offline errors —
   one JSONL line per event in each dashboard user's ~/.aios-usage.jsonl.
   NEVER search queries, terminal content, or transcripts. /api/usage (auth'd)
   serves per-day aggregates + a 30-min-gap session estimate.
2. Sasha UI redesign ("desert dusk companion") — the reach-test surface:
   greeting + plain-word chips + chat-as-hero, ops behind a drawer.
3. Hermes pane wrapper hardening: sheridanh's hermes rejects --cli (older CLI,
   falls back to a bare bash prompt — exactly wrong for a terminal-never user).
   New wrapper tries `hermes --cli` then `hermes chat`, and turns the tmux
   status bar OFF inside dashboard panes (the green ops bar).

Run with:  sudo bash /home/cvande/Github/CC/_lib/deploy_int4_telemetry.sh
```

## deploy_sasha_ui.sh
```
Deploy the current _lib/sasha_dashboard.py to Sheridan's live instance.
This round: compact header (smaller greeting/chips/margins — more room for
the chat) + /api/exec endpoint removed (unused by the new UI; hardening).

Run with:  sudo bash /home/cvande/Github/CC/_lib/deploy_sasha_ui.sh
```

## deploy_term_proxy.sh
```
Deploy the ttyd-behind-dashboard-auth fix.
  - rebinds the 4 dashboard ttyd services to loopback (127.0.0.1) + a --base-path
  - deploys the reworked sasha_dashboard.py into sheridanh's tree
  - restarts ttyd + both dashboards, then verifies nothing writable is on 0.0.0.0
Run with: sudo bash /home/cvande/Github/CC/_lib/deploy_term_proxy.sh
```

## fix_dashboard_pump.sh
```
Fix: dashboard terminals connect but render BLANK ("black box") for LAN clients.

Root cause: the terminal reverse-proxy pump (_pump) ran the sockets NON-blocking
and did `except OSError: return` around sendall. A full kernel send buffer makes
a non-blocking sendall raise BlockingIOError/EAGAIN (an OSError) — so the pump
treated normal backpressure as fatal and tore the websocket down. Over loopback
(127.0.0.1) the buffers are effectively unbounded so it never fires — which is
why it renders locally but blanks for real browsers over the LAN, where the
first tmux redraw burst fills the client's small TCP window. The rewritten pump
uses BLOCKING sockets (one thread per direction) so a full buffer WAITS
(backpressure) instead of dropping the connection.

The code fix is already in the two source files; this deploys + restarts.
  - craig: dashboard-craig runs /home/cvande/.../dashboard.py directly.
  - sasha: dashboard-sasha runs a COPY in sheridanh's tree — reinstall it.

Run with:  sudo bash /home/cvande/Github/CC/_lib/fix_dashboard_pump.sh
```

## fix_hermes_term.sh
```
Fix the dashboard "hermes" terminal green-then-red flap.

Root cause: the hermes ttyd pane ran bare `hermes`, whose default Node TUI
can't start (ui-tui/node_modules is missing), so it clears the screen and
exits in ~10ms.  The tmux session's command then completes, the pane dies,
and ttyd drops the websocket (close 1006) → the terminal goes green→red.
(The reverse-proxy + loopback security fix is fine — the bash pane is flawless.)

Fix: run hermes's classic REPL (`hermes --cli`, which holds the terminal
properly) via a tiny wrapper that falls back to a login shell if the REPL is
ever exited, so the pane never dies.  Only the two *hermes* units change; the
loopback bind + /term base-path (the security fix) are preserved verbatim.

Run with:  sudo bash /home/cvande/Github/CC/_lib/fix_hermes_term.sh
```

## fix_sasha_gw.sh
```
Fix sasha-gw-sheridanh: her hermes predates the `serve` subcommand (it has
`dashboard` only). Install a version-tolerant launcher that tries the
headless `serve` first, then `dashboard` with progressively fewer flags,
and logs which variant stuck. Also prints her hermes version + dashboard
flags so we know exactly what she runs.

Run with:  sudo bash /home/cvande/Github/CC/_lib/fix_sasha_gw.sh
```

## fix_sasha_gw2.sh
```
Round 2: her hermes dashboard is fine but has no prebuilt web dist
(--skip-build requires one). Sasha's chat only needs the gateway's /api/ws
and the token-injected index.html — not the SPA itself — so seed her tree
with the dist from cvande's checkout. If her server refuses it, fall back
to building it properly as her (needs npm, takes a few minutes).

Run with:  sudo bash /home/cvande/Github/CC/_lib/fix_sasha_gw2.sh
```

## fix_sasha_gw3.sh
```
Round 3: her gateway runs but ships embedded chat DISABLED
(__HERMES_DASHBOARD_EMBEDDED_CHAT__=false) — her build gates the chat
websocket behind --tui / HERMES_DASHBOARD_TUI=1. Enable it via the env var
(works regardless of argv), restart, then PROVE the /api/ws handshake with
a raw websocket client — not just a port check.

Run with:  sudo bash /home/cvande/Github/CC/_lib/fix_sasha_gw3.sh
```

## fix_term_crosstalk.sh
```
Fix: the dashboard "hermes" terminal intermittently renders a bare "404" (bash
renders fine).

Root cause: the terminal reverse-proxy (_proxy_term) tunnels a whole client TCP
connection to ONE loopback ttyd, chosen by the FIRST request's /term/<id>. It
forwarded the browser's `Connection: keep-alive` to ttyd verbatim, so ttyd held
the connection open — and the browser then REUSED that pooled connection for a
different terminal's asset (e.g. /term/hermes/ on a connection already pinned to
bash's ttyd on 8081). Each ttyd is launched with `-b /term/<id>` and 404s ANY
foreign path, so the reused request came back 404. Curl never triggers it (each
curl is a fresh, isolated connection) — only a real browser pooling connections
across the two iframes does. Which tab loses the race flips load-to-load.

Fix: for every non-WebSocket request the proxy now forces `Connection: close`
toward ttyd (and drops the client's keep-alive headers). ttyd closes after the
response and echoes `Connection: close`, so the browser never reuses a client
connection across terminals. WebSocket upgrades keep their persistent tunnel.
The code fix is already in both source files; this deploys + restarts.

Run with:  sudo bash /home/cvande/Github/CC/_lib/fix_term_crosstalk.sh
```

## fix_ttyd_renderer.sh
```
Fix: dashboard terminals connect ("green") but render BLANK on some browsers.

Root cause: ttyd 1.7.4 defaults to the **WebGL** xterm renderer. Browsers that
block or perturb WebGL — Brave with fingerprint shielding (default on, stricter
in private mode), or any browser with hardware acceleration off — fail to
create the WebGL context, so the terminal canvas never paints while the
websocket stays connected. It reproduced on a fresh never-cached laptop and
Brave private mode, but NOT in a browser with working WebGL — the tell is the
console line "[ttyd] WebGL renderer loaded".

Fix: force the **canvas** renderer (`-t rendererType=canvas`) on all four ttyd
units. Canvas 2D needs no GPU/WebGL and renders everywhere; verified on 1.7.4
(console then logs "[ttyd] canvas renderer loaded"). If a browser still blanks,
swap `canvas` -> `dom` below (pure-HTML renderer, ultimate compatibility).

Run with:  sudo bash /home/cvande/Github/CC/_lib/fix_ttyd_renderer.sh
```

## fix_tui_resize.sh
```
Fix the dashboard chat-pane rendering artifacts (dotted half-screen + redraw junk).

Root cause: tmux sizes a window to the SMALLEST attached client and paints the
unused region as dots on larger ones. The hermes pane gets attached from
different browsers/devices at different sizes, so someone always sees dots +
stale redraw fragments. tmux 3.4's `window-size latest` makes the window track
the most recently active client instead.

Scope: hermes chat panes only (via the shared wrapper). The bash panes are the
operator surface and keep default behavior.

Run with:  sudo bash /home/cvande/Github/CC/_lib/fix_tui_resize.sh
```

## migrate_sasha_ws.sh
```
Migrate Sheridan's live Sasha to the packaged, ws-native version.

What changes:
  * her dashboard now runs /usr/local/lib/sasha/dashboard.py (the cvp1/sasha
    package) with a ranch config — ends the two-divergent-copies drift
  * chat becomes NATIVE BUBBLES over hermes's /api/ws gateway (a new
    sasha-gw-sheridanh unit runs `hermes serve` on loopback :9121) —
    no more terminal-in-an-iframe, no update nags, no tmux artifacts
  * her auth file, port (:8085), and telemetry log stay EXACTLY as they are
    (INT-4 continuity); ttyd stays installed as the chat_mode:"term" fallback

Rollback: systemctl disable --now sasha-gw-sheridanh; restore the old
ExecStart in dashboard-sasha.service (kept as .bak); daemon-reload; restart.

Run with:  sudo bash /home/cvande/Github/CC/_lib/migrate_sasha_ws.sh
```

## sasha_polish.sh
```
Deploy the packaged dashboard update to Sheridan's live instance:
the Chat check now reads the gateway's embedded-chat flag + token, so the
pill can never say "All's well" while the conversation can't connect
(which is exactly what happened during today's rollout).
No gateway churn — only the web dashboard restarts; her chat session
reconnects by itself.

Run with:  sudo bash /home/cvande/Github/CC/_lib/sasha_polish.sh
```

## unify_craig.sh
```
Unify Craig's :8080 dashboard onto the sasha package — audience "pro":
same warm page + native ws chat as Sheridan's, PLUS the pro depth (skills
sidebar, bash/hermes terminal tabs, ranch checks, event feed) — all from
config, one code path for both dashboards from here on.

Preserved: port :8080, ~/.key/dash-auth credentials, ~/.aios-usage.jsonl
telemetry, the PUBLIC no-auth report pages (/weather /qrz /reports /learn
/frigate — other tools link to them), ttyd terminals as tabs.
Gone (consciously): the /api/exec shell endpoint and dynamic "+" terminals
(package never ships a shell endpoint), the ingest/board tabs (skills +
terminal cover them).
Rollback: restore dashboard-craig.service.bak, daemon-reload, restart.

Run with:  sudo bash /home/cvande/Github/CC/_lib/unify_craig.sh
```

## start_sasha.sh
```
Sasha Dashboard startup — run as sheridanh
Usage: sudo -u sheridanh bash /home/cvande/Github/CC/_lib/start_sasha.sh
Kill any existing Sasha processes
```

