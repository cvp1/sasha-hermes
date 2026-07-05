# Sasha

**An accessibility layer for hermes** (the Nous Research agent framework). One warm,
plain-language web page that puts a hermes agent in front of someone who will never
open a terminal: a greeting, a few plain-word action chips, and the conversation as
the hero. All the ops detail lives behind a quiet "under the hood" drawer.

Born on a ranch in the Arizona high desert as the front door to a household AI, for
a real second user who'd never touched a terminal — now generalized into this
package.

> **Honest status:** this requires an existing, working `hermes` install on the
> target user's PATH — the installer checks and stops without one. It has been
> extracted from (and hardened by) one real deployment; this generalized package
> has not yet been re-validated on a machine outside that household. Treat it as
> early. Basic Auth rides plain HTTP on the LAN — fine for a trusted home
> network, not for anything beyond it.

## What the person sees

- **"Good morning, ⟨name⟩"** — time-aware greeting, their place, the date
- **Chips in their words** — "Sort my email", "Find a note", "Weather" — never
  slash commands; coach marks suggest natural questions to type in the chat
- **The chat** — hermes itself, full-bleed, themed to match the page (warm paper,
  legible ANSI ramp), wrapped in tmux so a page refresh reconnects to the same
  conversation
- **Status in words** — "All's well", never RED/GREEN badges; checks, linked
  services, and agent notes live in the drawer

Design: "high-desert morning" — warm paper ground, ink brown, one clay action
color, sage for good news. Fraunces for the greeting, Atkinson Hyperlegible (a
typeface designed for legibility) for everything else. Light theme on research:
warm light reads safe and approachable for non-technical users; dark reads
"developer tool."

## Architecture & security posture

```
browser ──HTTP Basic Auth──▶ dashboard.py (:7790, LAN)
                               ├─ /            the page (native chat bubbles)
                               ├─ /api/*       checks · search · actions · telemetry
                               ├─ /gw/*        reverse-proxy ──▶ hermes serve (:7792, LOOPBACK ONLY)
                               │                                  └─ /api/ws JSON-RPC gateway
                               └─ /term/hermes reverse-proxy ──▶ ttyd (:7791, LOOPBACK ONLY, fallback)
                                                                  └─ tmux ─ hermes REPL
```

**The chat is native, not a terminal.** `chat_mode: "ws"` (the default the
installer writes) renders real conversation bubbles that speak hermes's own
`/api/ws` JSON-RPC gateway — the first-party seam hermes ships for web clients.
Streaming tokens, plain-language status ("Thinking…"), and approval requests
rendered as yes/no cards. The proxy rewrites `Host`/`Origin` to satisfy the
gateway's loopback rebinding guard. `chat_mode: "term"` keeps the legacy ttyd
terminal embed as a fallback.

- **Both backends bind loopback only** — the gateway and the terminal are never
  on the LAN; they are reached exclusively through the dashboard's
  authenticated reverse proxy (the installer verifies this and fails if not).
- **No shell endpoint** — the web API can only run the commands you list in
  `config.json` (`actions`), verbatim argv, no shell interpolation.
- **File reads are allowlisted** (`read_dirs`), realpath-checked.
- **Proxy details that matter** (learned live): blocking socket pumps (backpressure,
  not EAGAIN-drops) and `Connection: close` toward ttyd on non-WebSocket requests
  (browser connection-pooling otherwise cross-routes iframes). The pane wrapper sets
  `tmux window-size latest` so mixed-size clients don't leave dotted artifacts, and
  ttyd runs the canvas renderer so privacy browsers that block WebGL still paint.

## The me/ passport — graduation between Sasha surfaces

`~/ai-os/me/` (`WHOAMI.md` + `HOW-I-WORK.md`) is a shared identity schema:
the **same files** Sasha on Claude Code writes during its setup interview.
This layer seeds skeletons if they're absent (never overwrites) and wires
hermes to them via a marked block in `~/.hermes/SOUL.md` (`me_bridge.py`,
idempotent) — so the agent reads them every session, treats HOW-I-WORK's
rules as binding, and routes "that's not how I work" corrections back into
the files. The passport also carries **`CAPABILITIES.md`** — what's wired on each
surface (this layer writes its hermes skills + connectors from live state on
boot; the Claude Code product writes its command roster + connector). Either
Sasha reads the whole file and routes you to the surface that has what you
ask for.

A person can start on either surface and graduate to the other
without losing who they are — both Sashas read the same passport, and both
are told they are not the only writer (re-read before write, merge, never
overwrite).

## Telemetry (honest disclosure)

The page logs **aggregate event counts only** — page loads, chip clicks, a flag
that a search happened — one JSON line per event to a local file
(`~/.local/state/sasha/usage.jsonl`). **Never** search queries, chat content, or
transcripts; nothing leaves the machine. `/api/usage` (authenticated) serves
per-day totals. Turn it off with `"telemetry": false` in `config.json`.

## Install

Requires: a Linux host with systemd, `ttyd`, `tmux`, `python3` (stdlib only), and
`hermes` on the target user's login PATH.

```bash
sudo ./install.sh --user alice --name Alice --place "Home"
# prints the sign-in credential once; page at http://<host>:7790/
```

Then edit `~⟨user⟩/.config/sasha/config.json` to wire the optional pieces — see
`config.example.json`:

| Key | What it does |
|---|---|
| `actions` | argv commands surfaced as chips ("Sort my email"), output rendered as cards |
| `search_cmd` | argv command for "Find a note" (gets the query as last arg, returns JSON) |
| `weather_url` | adds a Weather chip linking out |
| `coach_chips` | chips that teach a natural question to ask in chat |
| `services` | link cards in the drawer |
| `notes_dir` / `read_dirs` | agent-notes panel + read allowlist |

Restart after config changes: `sudo systemctl restart sasha-web-<user>`.

Uninstall: `sudo ./uninstall.sh --user <user>` (keeps config + data).

**Known first-run issues (hit live, handled):**
- *`--skip-build … no web dist found`* — the gateway needs a prebuilt web UI.
  Fix once: `cd ~/.hermes/hermes-agent/web && npm install && npm run build`
  (or copy a `hermes_cli/web_dist/` from another machine on the same build).
- *Chat says "Reconnecting…" while status is green* — on hermes builds where
  embedded chat is opt-in, it must be enabled; the launcher exports
  `HERMES_DASHBOARD_TUI=1` for exactly this. The Chat check reads the
  gateway's own embedded-chat flag, so a disabled switch shows RED with a
  plain reason instead of a false "All's well."
- hermes CLIs drift across install tracks (`serve` vs `dashboard`-only,
  `--cli` vs `chat`): both launchers (`sasha-gw`, `sasha-term`) probe
  `--help` and adapt rather than assuming one CLI shape.

## Provenance

Proving ground: the CC ranch fleet (`cvp1`). The loopback+proxy posture, the pump
fixes, the tmux/ttyd hardening, and the palette all survived a real deployment
with a real non-technical user; this package is that deployment's
generalization, and hasn't itself re-run for that user yet (the ranch instance
migrating onto this package is the plan of record).

If the agent ever exits, the pane shows a plain-words holding screen and
retries — it never falls through to a shell.
