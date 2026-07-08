#!/usr/bin/env python3
"""Watchdog — always-on agent that bridges real-world events onto the event bus.

The only persistent process in the agent framework. Runs a tight polling loop
and publishes events that everything else subscribes to.

Design:
  - Single process, single thread, async-free — polling loop at 1Hz
  - Each watcher is a simple function that checks one source and returns events
  - Failed watchers degrade silently (the rest of the system keeps running)
  - Hermes-managed: started by cron @reboot or systemd

Usage:
    python3 watchdog.py                    # foreground (for testing)
    python3 watchdog.py --daemon           # fork to background
    python3 watchdog.py --dry-run          # print would-publish events

Signals:
    SIGTERM/SIGINT — graceful shutdown, flushes pending events
"""
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

# Add CC to path
CC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CC)
from _lib import event_bus

POLL_INTERVAL = 1.0  # seconds between full poll cycles
HEARTBEAT_INTERVAL = 300  # publish watchdog_heartbeat every 5 min


class Watchdog:
    """The always-on daemon. Run via .run()."""

    def __init__(self, bus=None, dry_run=False):
        self.bus = bus or event_bus.EventBus()
        self.dry_run = dry_run
        self._running = True
        self._last_heartbeat = 0
        self._last_tick_30m = 0
        self._last_tick_5am = None  # tracks by date, not timestamp

        signal.signal(signal.SIGTERM, self._signal)
        signal.signal(signal.SIGINT, self._signal)

    def _signal(self, signum, frame):
        self._running = False

    def _publish(self, source, type, payload=None):
        if self.dry_run:
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            print("[dry] %s  %s/%s  %s" % (ts, source, type,
                                            json.dumps(payload)[:120]))
            return
        self.bus.publish(source, type, payload)

    def _poll_tickers(self):
        """Fire time-based events."""
        now = time.time()

        # Heartbeat every 5 min
        if now - self._last_heartbeat >= HEARTBEAT_INTERVAL:
            self._last_heartbeat = now
            self._publish("watchdog", "heartbeat",
                          {"uptime": time.monotonic()})

        # every_30min tick
        if now - self._last_tick_30m >= 1800:
            self._last_tick_30m = now
            self._publish("watchdog", "every_30min", {})

        # daily_5am tick (once per calendar day)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        current_hour = datetime.now(timezone.utc).hour
        if self._last_tick_5am != today and current_hour == 12:  # 12 UTC = 5am MST
            self._last_tick_5am = today
            self._publish("watchdog", "daily_5am", {"date": today})

        # daily_6am tick
        if current_hour == 13:  # 13 UTC = 6am MST
            # (already covered by daily_5am date check)
            pass

    def run(self):
        """Main loop — poll all watchers at POLL_INTERVAL."""
        self._publish("watchdog", "watchdog_started", {})
        while self._running:
            try:
                self._poll_tickers()
            except Exception as e:
                # Ticker failure should never crash the watchdog
                if self.dry_run:
                    print("[dry] ticker error: %s" % e)
            time.sleep(POLL_INTERVAL)

        self._publish("watchdog", "watchdog_stopped",
                      {"uptime": time.monotonic()})
        print("watchdog: stopped", file=sys.stderr)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Always-on watchdog daemon")
    ap.add_argument("--dry-run", action="store_true",
                    help="print events instead of publishing")
    ap.add_argument("--daemon", action="store_true",
                    help="fork to background (TODO)")
    args = ap.parse_args()

    wd = Watchdog(dry_run=args.dry_run)
    try:
        wd.run()
    except KeyboardInterrupt:
        print("watchdog: keyboard interrupt", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
