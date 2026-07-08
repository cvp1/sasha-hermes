#!/usr/bin/env python3
"""Recent event-bus activity for the unified dashboard (pro audience).
Emits the package's `events_cmd` contract: {"events":[{source,type,ts}]}."""
import json, os, sys

sys.path.insert(0, os.path.join(os.path.expanduser("~"), "Github", "CC"))
try:
    from _lib.event_bus import EventBus
    evs = [{"source": e["source"], "type": e["type"], "ts": e["ts"]}
           for e in list(EventBus().subscribe(since_id=0, limit=100))[-30:]]
except Exception:
    evs = []
print(json.dumps({"events": evs}))
