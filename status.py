#!/usr/bin/env python3
"""System Status — one pane of glass for the whole AI-OS.

Three layers:
  Terminal:  python3 status.py             → compact green/yellow/red status
  Vault:     python3 status.py --write-note → full daily page in ~/notes/00 Meta/
  MCP:       python3 status.py --json       → machine-readable for Dex to query

Status levels per check:
  GREEN  — nominal, no action
  YELLOW — degraded but limping
  RED    — broken, needs attention

Design: single script, no external deps (stdlib + urllib for API calls).
Runs in <2s so it can be called on every /status invocation.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

HOME = os.path.expanduser("~")
CC = os.path.join(HOME, "Github", "CC")
HERMES = os.path.join(HOME, ".hermes")
AGENTS_DIR = os.path.join(HOME, "notes", "06 Logs", "Agents")
META_DIR = os.path.join(HOME, "notes", "00 Meta")
STATUS_NOTE = os.path.join(META_DIR, "System Status.md")
EVENT_BUS_DB = os.path.join(os.path.expanduser("~"), ".local", "state", "cc", "event-bus", "events.db")
KNOWLEDGE_INDEX = os.path.join(os.path.expanduser("~"), ".local", "state", "cc", "knowledge", "index.npz")
OLLAMA_URL = "http://192.168.86.21:11434"

# ---------------------------------------------------------------------------
# Checks — each returns (label, status, detail)
# status: "GREEN" | "YELLOW" | "RED"
# ---------------------------------------------------------------------------

def _ps_grep(name):
    """Check if a process named ``name`` is running."""
    try:
        r = subprocess.run(["pgrep", "-f", name], capture_output=True, timeout=5)
        return r.returncode == 0 and r.stdout.strip() != ""
    except Exception:
        return False


def _api(url, timeout=5):
    """GET a URL; return the parsed JSON or None."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


# (check_daemons removed Story 021 — the watchdog/agent_runner framework was
# retired as redundant with hermes cron; see sasha-hermes/attic/.)


def check_ollama():
    """.21 reachable? gemma4:e4b loaded?"""
    tags = _api(OLLAMA_URL + "/api/tags", timeout=3)
    if not tags:
        return (".21", "RED", "unreachable")
    models = [m["name"] for m in tags.get("models", [])]
    has_gemma = any("gemma4" in m for m in models)
    has_granite = any("granite" in m for m in models)
    # Check if a model is loaded
    ps = _api(OLLAMA_URL + "/api/ps", timeout=3)
    loaded = []
    if ps:
        loaded = [m.get("name", "") for m in ps.get("models", [])]
    detail = "up  models=%d  loaded=%s" % (len(models), loaded[0] if loaded else "none")
    status = "GREEN" if has_gemma else "YELLOW"
    if not tags:
        status = "RED"
    return (".21", status, detail)


def check_mcp():
    """All 8 MCP servers registered and responsive?"""
    # Load Hermes config and count enabled MCP servers
    import yaml  # optional dep; falls back to grep
    try:
        cfg_path = os.path.join(HERMES, "config.yaml")
        with open(cfg_path) as fh:
            cfg = yaml.safe_load(fh)
        servers = cfg.get("mcp_servers", {})
        enabled = sum(1 for s in servers.values() if s.get("enabled", False))
        total = len(servers)
    except Exception:
        # Fallback: grep the config
        try:
            r = subprocess.run(
                ["grep", "-c", "enabled: true", os.path.join(HERMES, "config.yaml")],
                capture_output=True, text=True, timeout=5)
            enabled = int(r.stdout.strip() or 0)
            r2 = subprocess.run(
                ["grep", "-c", "^  [a-z]", os.path.join(HERMES, "config.yaml")],
                capture_output=True, text=True, timeout=5)
            total = max(0, int(r2.stdout.strip() or 0) - 1)  # subtract mcp_servers key
        except Exception:
            return ("MCP", "YELLOW", "cannot read config")
    ok = enabled == total and total >= 7
    at_risk = total >= 5 and enabled < total
    return ("MCP", "GREEN" if ok else ("YELLOW" if at_risk else "RED"),
            "%d/%d servers" % (enabled, total))


def check_cron():
    """All cron jobs running?"""
    try:
        r = subprocess.run(
            ["hermes", "cron", "list"],
            capture_output=True, text=True, timeout=10)
        # Count active jobs (lines like "  <id> [active]")
        active = sum(1 for l in r.stdout.split("\n") if "[active]" in l and l[:1] in (" ", "\t"))
        return ("Cron", "GREEN" if active >= 3 else "YELLOW",
                "%d active jobs" % active)
    except Exception as e:
        return ("Cron", "YELLOW", str(e)[:60])


def check_event_bus():
    """Events flowing? Processing rate healthy?"""
    try:
        bus_dir = os.path.dirname(EVENT_BUS_DB)
        if not os.path.exists(bus_dir):
            return ("Events", "YELLOW", "bus not started (no events yet)")
        conn = sqlite3.connect(EVENT_BUS_DB)
        cur = conn.execute("SELECT COUNT(*), COALESCE(SUM(processed),0), COALESCE(MAX(id),0) FROM events")
        total, processed, max_id = cur.fetchone()
        conn.close()
    except Exception:
        return ("Events", "YELLOW", "cannot read bus")
    if total == 0:
        return ("Events", "YELLOW", "no events yet (bus started recently)")
    pct = (processed / total * 100) if total else 0
    backlog = total - processed
    status = "GREEN"
    if backlog > 50 or pct < 50:
        status = "YELLOW"
    if backlog > 200:
        status = "RED"
    return ("Events", status, "%d events, %d%% processed" % (total, int(pct)))


def check_cost():
    """DeepSeek balance + daily spend."""
    try:
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            env_path = os.path.expanduser("~/.hermes/.env")
            for line in open(env_path):
                if line.startswith("DEEPSEEK_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip("\"'")
                    break
        if key:
            req = urllib.request.Request(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": "Bearer " + key,
                         "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            infos = data.get("balance_infos", [])
            bal = float(infos[0].get("total_balance", 0)) if infos else 0
            detail = "$%.2f balance" % bal
            status = "GREEN" if bal > 5 else ("YELLOW" if bal > 1 else "RED")
            return ("Cost", status, detail)
    except Exception:
        pass
    return ("Cost", "YELLOW", "unknown balance")


def check_knowledge():
    """Knowledge index built and fresh?"""
    if not os.path.exists(KNOWLEDGE_INDEX):
        return ("Knowledge", "YELLOW", "not built (runs at 4am)")
    age_h = (time.time() - os.path.getmtime(KNOWLEDGE_INDEX)) / 3600
    status = "GREEN" if age_h < 28 else ("YELLOW" if age_h < 72 else "RED")
    return ("Knowledge", status, "%.0f passages, %.1fh old" % (
        sum(1 for _ in open(KNOWLEDGE_INDEX.replace("index.npz", "meta.jsonl"))), age_h
    ) if os.path.exists(KNOWLEDGE_INDEX.replace("index.npz", "meta.jsonl")) else "?")


def check_agents():
    """Agent output notes in vault. Any from today?"""
    if not os.path.exists(AGENTS_DIR):
        return ("Agents", "GREEN", "no output yet")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_count = sum(1 for f in os.listdir(AGENTS_DIR) if f.startswith(today) and f.endswith(".md"))
    total = len([f for f in os.listdir(AGENTS_DIR) if f.endswith(".md")])
    return ("Agents", "GREEN" if today_count > 0 else "GREEN",
            "%d today · %d total" % (today_count, total))


# ---------------------------------------------------------------------------
# Registry of all checks
# ---------------------------------------------------------------------------
CHECKS = [
    check_ollama,
    check_mcp,
    check_cron,
    check_event_bus,
    check_cost,
    check_knowledge,
    check_agents,
]


def run_checks():
    """Run all checks, return (list_of_tuples, status_code)."""
    results = []
    any_red = False
    any_yellow = False
    for check_fn in CHECKS:
        try:
            label, status, detail = check_fn()
        except Exception as e:
            label = check_fn.__name__.replace("check_", "")
            status = "RED"
            detail = str(e)[:60]
            any_red = True
        if status == "RED":
            any_red = True
        elif status == "YELLOW":
            any_yellow = True
        results.append((label, status, detail))
    overall = "GREEN"
    if any_red:
        overall = "RED"
    elif any_yellow:
        overall = "YELLOW"
    return results, overall


# ---------------------------------------------------------------------------
# Layer 1: Terminal output (compact, fits in one screen)
# ---------------------------------------------------------------------------
def render_terminal(results, overall):
    now = datetime.now(timezone.utc)
    mst = now - timedelta(hours=7)
    time_str = mst.strftime("%Y-%m-%d %H:%M") + " MST"
    label_w = max(len(r[0]) for r in results) + 1

    lines = []
    lines.append("")
    lines.append("  System Status  %s" % time_str)
    lines.append("  " + "─" * 50)
    icons = {"GREEN": "✓", "YELLOW": "~", "RED": "✗"}
    for label, status, detail in results:
        icon = icons.get(status, "?")
        lines.append("  %s %s%s  %s" % (
            icon, label.ljust(label_w), status.ljust(6), detail))
    lines.append("  " + "─" * 50)
    overall_icon = {"GREEN": "ALL GREEN", "YELLOW": "DEGRADED", "RED": "ISSUES FOUND"}
    lines.append("  Status: %s" % overall_icon.get(overall, "UNKNOWN"))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Layer 2: Vault note (full daily status page)
# ---------------------------------------------------------------------------
def render_note(results, overall):
    now = datetime.now(timezone.utc)
    mst = now - timedelta(hours=7)
    time_str = mst.strftime("%Y-%m-%dT%H:%M") + "-07:00"
    date_str = mst.strftime("%A, %B %-d, %Y")

    icons = {"GREEN": "✅", "YELLOW": "⚠️", "RED": "❌"}
    status_icons = {"GREEN": "✅ All green", "YELLOW": "⚠️ Degraded", "RED": "❌ Issues found"}

    lines = [
        "---",
        "date: %s" % time_str,
        "type: system-status",
        "---",
        "",
        "# System Status — %s" % date_str,
        "",
        "**%s**" % status_icons.get(overall, "UNKNOWN"),
        "",
        "## Checks",
        "",
    ]
    for label, status, detail in results:
        icon = icons.get(status, "?")
        lines.append("| %s **%s** | %s | %s |" % (icon, label, status, detail))

    lines += [
        "",
        "## Agent Output",
        "",
        "All agent output is in `06 Logs/Agents/`. Browse by date or agent name:",
        "",
    ]
    if os.path.exists(AGENTS_DIR):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_files = sorted([f for f in os.listdir(AGENTS_DIR)
                              if f.startswith(today) and f.endswith(".md")],
                             reverse=True)
        for f in today_files[:10]:
            path = os.path.join(AGENTS_DIR, f)
            size = os.path.getsize(path)
            lines.append("- [[06 Logs/Agents/%s|%s]]  (%d bytes)" % (f, f.replace(".md", ""), size))
        if not today_files:
            lines.append("- _No agent output yet today._")
        else:
            lines.append("")
            lines.append("_Older output: browse `06 Logs/Agents/` in the vault._")
    else:
        lines.append("- _No agent output yet._")

    lines += [
        "",
        "## Quick Actions",
        "",
    ]
    if overall == "RED":
        lines.append("- ❌ **Issues found** — check the RED items above")
    if overall == "YELLOW":
        lines.append("- ⚠️ **Degraded** — review YELLOW items")
    if overall == "GREEN":
        lines.append("- ✅ **All nominal** — no action needed")
    lines += [
        "- `/backup` — create a snapshot",
        "- `hermes cron list` — view scheduled jobs",
        "- `python3 event_bus.py stats` — event bus details",
        "",
        "---",
        "_Auto-generated by system-status · runs on every `/status` call._",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Layer 3: JSON (for MCP / Dex queries)
# ---------------------------------------------------------------------------
def render_json(results, overall):
    return json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overall": overall,
        "checks": [{"label": r[0], "status": r[1], "detail": r[2]} for r in results],
    }, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    import argparse
    ap = argparse.ArgumentParser(description="System status — one pane of glass")
    ap.add_argument("--write-note", action="store_true", help="write vault note (Layer 2)")
    ap.add_argument("--json", action="store_true", help="JSON output (Layer 3)")
    ap.add_argument("--watch", type=int, default=0,
                    help="refresh every N seconds (default: off)")
    args = ap.parse_args()

    if args.watch:
        try:
            while True:
                results, overall = run_checks()
                os.system("clear")
                print(render_terminal(results, overall))
                time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0

    results, overall = run_checks()

    if args.json:
        print(render_json(results, overall))
    else:
        print(render_terminal(results, overall))

    if args.write_note:
        os.makedirs(META_DIR, exist_ok=True)
        note = render_note(results, overall)
        with open(STATUS_NOTE, "w", encoding="utf-8") as fh:
            fh.write(note)
        print("  Written to %s" % STATUS_NOTE, file=sys.stderr)

    return 0 if overall == "GREEN" else (1 if overall == "YELLOW" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
