#!/usr/bin/env python3
"""Ranch-specific health checks for the unified Sasha dashboard (pro audience).

Emits the package's `checks_cmd` JSON contract: a list of
{label, status(GREEN|YELLOW|RED), detail}. These are the CC checks the old
_lib/dashboard.py carried inline (daemons, .21 ollama, DeepSeek balance,
events db, knowledge index, agent notes) — now a standalone emitter so the
dashboard code stays generic.
"""
import json, os, sqlite3, subprocess, sys, time, urllib.request
from datetime import datetime

HOME = os.path.expanduser("~")
CC = os.path.join(HOME, "Github", "CC")
AGENTS_DIR = os.path.join(HOME, "notes", "06 Logs", "Agents")


def _pgrep(name):
    try:
        r = subprocess.run(["pgrep", "-f", name], capture_output=True, text=True, timeout=3)
        return r.returncode == 0 and r.stdout.strip() != ""
    except Exception:
        return False


def _api_json(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


# (daemons() check removed Story 021 — watchdog/agent_runner framework retired.)


def ollama():
    tags = _api_json("http://192.168.86.21:11434/api/tags", 2)
    if tags:
        loaded = _api_json("http://192.168.86.21:11434/api/ps", 2)
        l = loaded["models"][0]["name"] if loaded and loaded.get("models") else "cold"
        return (".21", "GREEN", f"up · {len(tags.get('models', []))} models · {l}")
    return (".21", "RED", "unreachable")


def events_db():
    try:
        db = os.path.join(CC, "_lib", "event_bus_data", "events.db")
        if os.path.exists(db):
            conn = sqlite3.connect(db)
            t, p = conn.execute("SELECT COUNT(*),COALESCE(SUM(processed),0) FROM events").fetchone()
            conn.close()
            pct = int(p / t * 100) if t else 0
            return ("Events", "GREEN" if pct >= 50 else "YELLOW", f"{t} · {pct}%")
        return ("Events", "YELLOW", "no events")
    except Exception:
        return ("Events", "YELLOW", "?")


def cost():
    try:
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            for l in open(os.path.join(HOME, ".hermes", ".env")):
                if l.startswith("DEEPSEEK_API_KEY="):
                    key = l.split("=", 1)[1].strip().strip("\"'")
        req = urllib.request.Request("https://api.deepseek.com/user/balance",
                                     headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=4) as r:
            infos = json.loads(r.read()).get("balance_infos", [])
        bal = float(infos[0]["total_balance"]) if infos else 0
        return ("Cost", "GREEN" if bal > 5 else "YELLOW", f"${bal:.2f}")
    except Exception:
        return ("Cost", "YELLOW", "?")


def knowledge():
    ki = os.path.join(CC, "_lib", "knowledge_index_data", "index.npz")
    mi = os.path.join(CC, "_lib", "knowledge_index_data", "meta.jsonl")
    if os.path.exists(ki):
        ah = (time.time() - os.path.getmtime(ki)) / 3600
        n = sum(1 for _ in open(mi)) if os.path.exists(mi) else 0
        return ("Knowledge", "GREEN" if ah < 28 else "YELLOW", f"{n} passages · {ah:.0f}h")
    return ("Knowledge", "YELLOW", "not built")


def agents():
    if os.path.exists(AGENTS_DIR):
        t = datetime.now().strftime("%Y-%m-%d")
        td = sum(1 for f in os.listdir(AGENTS_DIR) if f.startswith(t))
        return ("Agents", "GREEN", f"{td} today · {len(os.listdir(AGENTS_DIR))} total")
    return ("Agents", "GREEN", "no output")


if __name__ == "__main__":
    from concurrent.futures import ThreadPoolExecutor, as_completed
    rows = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(f) for f in (ollama, events_db, cost, knowledge, agents)]
        for f in as_completed(futs, timeout=9):
            try:
                l, s, d = f.result()
                rows.append({"label": l, "status": s, "detail": d})
            except Exception:
                pass
    print(json.dumps(rows))
