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
                               ├─ /            the page
                               ├─ /api/*       checks · search · actions · telemetry
                               └─ /term/hermes reverse-proxy ──▶ ttyd (:7791, LOOPBACK ONLY)
                                                                  └─ tmux ─ hermes
```

- **ttyd binds loopback only** — the writable terminal is never on the LAN; it is
  reached exclusively through the dashboard's authenticated reverse proxy.
- **No shell endpoint** — the web API can only run the commands you list in
  `config.json` (`actions`), verbatim argv, no shell interpolation.
- **File reads are allowlisted** (`read_dirs`), realpath-checked.
- **Proxy details that matter** (learned live): blocking socket pumps (backpressure,
  not EAGAIN-drops) and `Connection: close` toward ttyd on non-WebSocket requests
  (browser connection-pooling otherwise cross-routes iframes). The pane wrapper sets
  `tmux window-size latest` so mixed-size clients don't leave dotted artifacts, and
  ttyd runs the canvas renderer so privacy browsers that block WebGL still paint.

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
sudo ./install.sh --user sheridanh --name Sheridan --place "The ranch"
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

## Provenance

Proving ground: the CC ranch fleet (`cvp1`). The loopback+proxy posture, the pump
fixes, the tmux/ttyd hardening, and the palette all survived a real deployment
with a real non-technical user; this package is that deployment's
generalization, and hasn't itself re-run for that user yet (the ranch instance
migrating onto this package is the plan of record).

If the agent ever exits, the pane shows a plain-words holding screen and
retries — it never falls through to a shell.
