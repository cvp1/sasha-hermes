#!/usr/bin/env python3
"""Print recent event-bus activity for the dashboard's events_cmd: {"events":[{source,type,ts}]}."""
import json, os, sys

sys.path.insert(0, os.path.join(os.path.expanduser("~"), "Github", "CC"))
try:
    from _lib.event_bus import EventBus
    evs = [{"source": e["source"], "type": e["type"], "ts": e["ts"]}
           for e in list(EventBus().subscribe(since_id=0, limit=100))[-30:]]
except Exception:
    evs = []
print(json.dumps({"events": evs}))
