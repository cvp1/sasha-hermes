# Agent Framework — Event Bus + Watchdog

## The problem

Right now every agent is a cron script that polls. The inbox triage polls
every 30 minutes even when nothing changed. The knowledge gardener runs daily
and has no idea a signal scan just completed. No agent knows what another agent
is doing.

Fixing that means shifting from **poll** to **event-driven**.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  Event Bus (SQLite)                   │
│  An append-only log of structured events that every   │
│  agent reads from. Agents subscribe to event types.   │
└──────┬────────────┬──────────────┬───────────────────┘
       │            │              │
  ┌────▼───┐  ┌─────▼─────┐  ┌────▼────────┐
  │Watchdog│  │  Triage   │  │  Gardener   │  ...
  │(always │  │(event-    │  │(event-      │
  │  on)   │  │ driven)   │  │ driven)     │
  └────────┘  └───────────┘  └─────────────┘
```

## Event Bus (`_lib/event_bus.py`)

A local SQLite database with one table:

```sql
CREATE TABLE events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT NOT NULL,        -- ISO timestamp
  source    TEXT NOT NULL,        -- "inbox_triage", "watchdog", "signal_scan", ...
  type      TEXT NOT NULL,        -- "new_email", "motion_detected", "daily_tick", ...
  payload   TEXT NOT NULL,        -- JSON blob with event-specific data
  processed INTEGER DEFAULT 0     -- 0=new, 1=picked up by at least one subscriber
);
```

API:

```python
event_bus.publish(source, type, payload={})
  → appends event, returns event_id

event_bus.subscribe(source=None, type=None, since_id=0)
  → yields new events matching source/type since last read

event_bus.ack(event_id)
  → marks event processed
```

The event bus stays simple. No MQTT, no Redis — just SQLite with a polling
loop. It's already fast enough for the volumes we'd generate (hundreds/day,
not millions/second).

## Watchdog Agent (`watchdog.py`)

The **only** always-on process. Runs as a Hermes-managed daemon
(`proton-drive` style, background subprocess). It bridges real-world events
onto the bus:

| Watcher | What it watches | Events published |
|---|---|---|
| **Frigate watcher** | MQTT on cam events (`doorbell`, `driveway`, `backyard`) | `motion_detected`, `person_detected`, `camera_offline` |
| **HA sensor watcher** | Home Assistant WebSocket (temperature, humidity, power) | `sensor_alert`, `sensor_recovered` |
| **File watcher** | `inotify` on vault + inbox changes | `note_changed`, `signal_scan_complete`, `inbox_item_added` |
| **Cron ticker** | Internal timer | `every_30min`, `daily_5am`, `daily_6am` |
| **Network watcher** | Ping .21, check Starlink latency | `node_offline`, `node_recovered`, `latency_spike` |

Design:
```python
class Watchdog:
    def run(self):
        while True:
            self.poll_frigate()    # check MQTT
            self.poll_ha()         # check WebSocket
            self.poll_files()      # check inotify
            self.poll_ticks()      # fire time events
            time.sleep(0.5)        # 500ms loop — catches things fast
```

## What this enables

**Phase 1 — Move existing agents to event-driven:**
- Inbox triage: subscribes to `new_email` instead of polling every 30min.
  Runs instantly when mail arrives. No wasted checks.
- Knowledge gardener: subscribes to `daily_5am` + `signal_scan_complete`.
  Runs right after signal scan finishes, has freshest data.
- Knowledge index: subscribes to `note_changed` + `signal_scan_complete`.
  Rebuilds only when something actually changed, not daily on schedule.

**Phase 2 — New agents that weren't possible before:**
- **Pattern learner**: subscribes to everything. After 30 days it can say
  "your inbox volume dropped 40% on Fridays" or "the backyard camera
  triggers suspiciously at 3am twice a week."
- **Anomaly detector**: compares current events against learned baselines.
  "Your inbox has 3 unread urgent items from Goldman — that's unusual
  for a Tuesday afternoon."
- **Cross-agent actions**: The gardener notices a stale note about hydroponics.
  The pattern learner notices signal scans have been talking about new
  indoor farming techniques. Together they propose a richer update.

**Phase 3 — Agent coordination:**
- Agents can react to each other's outputs. The triage flags a Goldman email.
  The watchdog notices it's a partner escalation. The pattern learner
  knows you had a similar one 6 weeks ago. All three collaborate to
  produce a single proactive briefing — "Goldman escalation pattern
  detected, here's what worked last time."

## Build plan (one session, ~2-3 hours)

```
1. event_bus.py     — SQLite event log, publish/subscribe/ack   ~45 min
2. watchdog.py      — always-on daemon, Frigate/HA/file watchers ~1.5h
3. Migrate triage   — inbox_triage.py subscribes to new_email     ~20 min
4. Migrate gardener — knowledge_gardener.py subscribes to ticks   ~15 min
5. systemd unit     — keep watchdog alive on reboot              ~10 min
```

## Files

- `_lib/event_bus.py`      — the bus
- `_lib/watchdog.py`       — the watchdog daemon
- `watchdog.service`       — systemd user unit
- `inbox_triage.py`        — updated to event-driven
- `knowledge_gardener.py`  — updated to event-driven
