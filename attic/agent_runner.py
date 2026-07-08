#!/usr/bin/env python3
"""Agent runner — subscribe to event bus and dispatch to agent scripts.

Each agent is a function that takes a single event dict and returns
(alert_type, alert_body) or None. The runner handles subscription, ack,
and alert delivery.

Usage:
    python3 agent_runner.py                    # listen and dispatch (foreground)
    python3 agent_runner.py --dry-run          # show what would happen

The runner is started by the watchdog (or systemd) and runs forever.
"""
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

CC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CC)
from _lib import event_bus, mail

AGENTS_DIR = os.path.join(os.path.expanduser("~"), "notes", "06 Logs", "Agents")


def _write_note(agent_name, body):
    """Write agent output as a dated vault note. Returns the note path."""
    os.makedirs(AGENTS_DIR, exist_ok=True)
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M")
    slug = "%s-%s-%s" % (date_str, agent_name.replace(" ", "-"), time_str)
    path = os.path.join(AGENTS_DIR, "%s.md" % slug)
    content = (
        "---\n"
        "date: %s\n"
        "agent: %s\n"
        "---\n\n"
        "# %s — %s\n\n%s\n"
    ) % (now.isoformat(timespec="seconds"), agent_name,
         agent_name, date_str, body)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def _run_agent(script_path, *args):
    """Run an agent script and return its stdout output."""
    result = subprocess.run(
        [sys.executable, script_path] + list(args),
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-300:])
    return result.stdout.strip()


def handle_event(event):
    """Route an event to the right agent. Returns None or (alert_body, alert_type)."""
    etype = event.get("type")
    source = event.get("source")

    # Inbox triage: every_30min tick
    if etype == "every_30min":
        try:
            triage_args = ["--dry-run"] if "--dry-run" in sys.argv else []
            output = _run_agent(
                os.path.join(CC, "_lib", "inbox_triage.py"),
                *triage_args,
            )
            note_path = _write_note("inbox-triage", output)
            print("  inbox-triage → %s" % note_path, file=sys.stderr)
            if "urgent" in output.lower() and "0 urgent" not in output:
                output = _run_agent(os.path.join(CC, "_lib", "inbox_triage.py"))
                note_path = _write_note("inbox-triage", output)
                return ("Inbox triage — urgent items", note_path)
        except Exception as e:
            note_path = _write_note("inbox-triage-error", str(e))
            return ("Inbox triage error", note_path)

    # Knowledge gardener: daily_5am tick
    if etype == "daily_5am":
        alerts = []
        try:
            # Check if there are proposals with a dry-run
            output = _run_agent(
                os.path.join(CC, "_lib", "knowledge_gardener.py"),
                "--dry-run",
            )
            if "proposal" in output.lower() and "0 proposal" not in output:
                output = _run_agent(os.path.join(CC, "_lib", "knowledge_gardener.py"))
                note_path = _write_note("knowledge-gardener", output)
                alerts.append(("Gardener proposals", note_path))
            else:
                _write_note("knowledge-gardener", "No stale notes to garden today.")
        except Exception as e:
            alerts.append(("Gardener error", str(e)))

        # Pattern learner: daily_5am tick (runs after gardener regardless)
        try:
            learner_args = ["--publish"]
            if "--dry-run" in sys.argv:
                learner_args.insert(0, "--dry-run")
            learner_out = _run_agent(
                os.path.join(CC, "_lib", "pattern_learner.py"),
                *learner_args,
            )
            note_path = _write_note("pattern-learner", learner_out)
            alerts.append(("Pattern learner", note_path))
        except Exception as e:
            alerts.append(("Pattern learner error", str(e)))

        if alerts:
            summary = "\n".join("- %s: %s" % a for a in alerts)
            return ("Daily agents", summary)

    # Heartbeat: log silently
    if etype == "heartbeat":
        return None

    return None


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Agent runner — event-driven dispatch")
    ap.add_argument("--dry-run", action="store_true", help="show actions without running")
    ap.add_argument("--bus-path", default=None, help="event bus db path")
    args = ap.parse_args()

    bus = event_bus.EventBus(args.bus_path)
    since = bus.last_id()
    print("agent-runner: listening for events since id=%d" % since,
          file=sys.stderr)

    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))

    for event in bus.subscribe_blocking(since_id=since, poll_interval=0.5):
        try:
            result = handle_event(event)
            if result:
                alert_type, alert_body = result
                if args.dry_run:
                    print("[dry] %s: %s" % (alert_type, alert_body[:100]),
                          file=sys.stderr)
                else:
                    print("%s: %s" % (alert_type, alert_body[:100]),
                          file=sys.stderr)
            bus.ack(event["id"])
        except Exception as e:
            print("agent-runner: error handling %s/%s: %s"
                  % (event.get("source"), event.get("type"), e),
                  file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
