#!/usr/bin/env python3
"""Sasha — an accessibility layer for hermes.

A warm, plain-language web front door to a hermes agent for someone who will
never open a terminal: a greeting, a few plain-word action chips, and the
hermes chat as the hero. Ops detail lives behind an "under the hood" drawer.

Config: ~/.config/sasha/config.json (or $SASHA_CONFIG). See config.example.json.
Security posture: ttyd bound to loopback only, reverse-proxied behind this
server's Basic Auth; no shell endpoint; file reads restricted to an allowlist.
Telemetry: aggregate event COUNTS only (no queries, content, or transcripts),
local JSONL, off with "telemetry": false.
"""
import json, os, subprocess, sys, time, urllib.request, socket, threading, shutil
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import base64

HOME = os.path.expanduser("~")
CONFIG_PATH = os.environ.get("SASHA_CONFIG", os.path.join(HOME, ".config", "sasha", "config.json"))
try:
    CONFIG = json.load(open(CONFIG_PATH))
except Exception:
    CONFIG = {}

NAME       = CONFIG.get("name", "friend")
PLACE      = CONFIG.get("place", "Home")
AUTH_FILE  = os.path.expanduser(CONFIG.get("auth_file", "~/.config/sasha/auth"))
USAGE_PATH = os.path.expanduser(CONFIG.get("usage_file", "~/.local/state/sasha/usage.jsonl"))
TELEMETRY  = bool(CONFIG.get("telemetry", True))
SEARCH_CMD = CONFIG.get("search_cmd")            # argv list; the query is appended
WEATHER_URL = CONFIG.get("weather_url")          # optional chip -> external page
NOTES_DIR  = os.path.expanduser(CONFIG["notes_dir"]) if CONFIG.get("notes_dir") else None
READ_DIRS  = [os.path.expanduser(d) for d in CONFIG.get("read_dirs", [])] or ([NOTES_DIR] if NOTES_DIR else [])
# actions: {"id": {"cmd": [argv], "chip": "Sort my email", "emoji": "&#128229;",
#                  "label": "Your email", "busy": "Sorting your email…"}}
ACTIONS    = CONFIG.get("actions", {})
# coach chips: [{"id","emoji","chip","text"}] — text is the coach-mark HTML
COACH_CHIPS = CONFIG.get("coach_chips", [
    {"id": "anything", "emoji": "&#128172;", "chip": "What can you do?",
     "text": "Just type a question in the chat below, like <q>what can you help me with?</q> &mdash; I answer in plain English."}
])
SERVICES   = CONFIG.get("services", [])          # [{"href","icon","title","desc"}]

# ---- Audience: "novice" (default) or "pro" ----
# Pro is an AUDIENCE, not a fork: same page, opt-in depth. Pro adds a skills
# sidebar (auto-discovered), terminal tabs beside the chat, an activity feed,
# and extra owner-defined checks. The novice surface never shows any of it.
AUDIENCE   = CONFIG.get("audience", "novice")
IS_PRO     = AUDIENCE == "pro"
SKILLS_DIR = os.path.expanduser(CONFIG.get("skills_dir", "~/.hermes/skills")) if IS_PRO else None
CHECKS_CMD = CONFIG.get("checks_cmd") if IS_PRO else None   # argv -> JSON [{label,status,detail}]
EVENTS_CMD = CONFIG.get("events_cmd") if IS_PRO else None   # argv -> JSON {"events":[{source,type,ts}]}
PUBLIC_PATHS = tuple(CONFIG.get("public_paths", []))        # static_dirs prefixes served WITHOUT auth

# Loopback-bound ttyd terminals, reverse-proxied under /term/<id>/ behind this
# dashboard's Basic Auth. Nothing writable listens on 0.0.0.0 anymore.
TERM_PORTS = {"hermes": int(CONFIG.get("term_port", 7791))}
# Pro may define several terminals: {"bash": 8081, "hermes": 8082}
if IS_PRO:
    for _tn, _tp in CONFIG.get("terminals", {}).items():
        TERM_PORTS[_tn] = int(_tp)

# Chat transport: "ws" = native chat bubbles over hermes's /api/ws JSON-RPC
# gateway (`hermes serve`, loopback) — the first-party seam built for web
# clients. "term" = legacy ttyd terminal embed (fallback).
CHAT_MODE = CONFIG.get("chat_mode", "term")
GW_PORT   = int(CONFIG.get("gw_port", 9119))

# ---- INT-4 usage telemetry: aggregate EVENT COUNTS only. One JSONL line per
# UI event ({ts, e, ip}) — never search queries, terminal content, or any
# transcript. /api/usage serves per-day aggregates + a 30-min-gap session
# estimate for the INT-4 reach report.
_usage_lock = threading.Lock()

def track(event, ip=""):
    if not TELEMETRY:
        return
    e = (event or "")[:48]
    if not e or not e.replace(":", "").replace("-", "").replace("_", "").replace(".", "").isalnum():
        return
    try:
        os.makedirs(os.path.dirname(USAGE_PATH), exist_ok=True)
        with _usage_lock, open(USAGE_PATH, "a") as f:
            f.write(json.dumps({"ts": int(time.time()), "e": e, "ip": ip}) + "\n")
    except OSError:
        pass

def usage_summary():
    days, stamps = {}, {}
    try:
        with open(USAGE_PATH) as f:
            for ln in f:
                try: r = json.loads(ln)
                except ValueError: continue
                d = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")
                days.setdefault(d, {}); days[d][r["e"]] = days[d].get(r["e"], 0) + 1
                stamps.setdefault(d, []).append(r["ts"])
    except OSError:
        pass
    sessions = {}
    for d, ts in stamps.items():
        ts.sort()
        sessions[d] = sum(1 for i, t in enumerate(ts) if i == 0 or t - ts[i-1] > 1800)
    return {"days": days, "sessions": sessions}

# ---- Cached values (refresh every 60s) ----
_cache = {"mcp": None, "mcp_ts": 0, "cron": None, "cron_ts": 0}

def _pgrep(name):
    try: r = subprocess.run(["pgrep","-f",name], capture_output=True, text=True, timeout=3); return r.returncode == 0 and r.stdout.strip()!=""
    except: return False

def _api_json(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r: return json.loads(r.read())
    except: return None

def _check_chat():
    """GREEN only when the chat can actually connect — not just when a port
    answers. In ws mode that means the gateway serves its page AND embedded
    chat is enabled (a live gateway with the chat switch off once read
    'All's well' while the conversation couldn't connect — never again)."""
    if CHAT_MODE == "ws":
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{GW_PORT}/", timeout=3) as r:
                body = r.read(65536).decode("utf-8", "replace")
        except OSError:
            return ("Chat", "RED", "chat offline")
        if "__HERMES_DASHBOARD_EMBEDDED_CHAT__=false" in body:
            return ("Chat", "RED", "chat switch is off on the agent")
        if "__HERMES_SESSION_TOKEN__" not in body:
            return ("Chat", "YELLOW", "agent is starting up")
        return ("Chat", "GREEN", "ready")
    port_ok = False
    try:
        s = socket.create_connection(("127.0.0.1", TERM_PORTS["hermes"]), timeout=2); s.close()
        port_ok = True
    except OSError:
        pass
    agent_ok = _pgrep("hermes")
    if port_ok and agent_ok:
        return ("Chat", "GREEN", "ready")
    if port_ok:
        return ("Chat", "YELLOW", "waking the agent up")
    return ("Chat", "RED", "chat pane offline")

def _check_mcp():
    global _cache
    now = time.time()
    if _cache["mcp"] and now - _cache["mcp_ts"] < 60:
        return _cache["mcp"]
    try:
        y = Path(os.path.join(HOME, ".hermes", "config.yaml")).read_text()
        lines = y.split("\n")
        in_mcp = False; servers = 0; enabled = 0
        for i, l in enumerate(lines):
            if l.strip() == "mcp_servers:":
                in_mcp = True; continue
            if in_mcp:
                stripped = l.strip()
                if stripped and not stripped.startswith("#") and ":" in stripped and not stripped.startswith("-"):
                    key = stripped.split(":")[0].strip()
                    if key and " " not in key and stripped.endswith(":"):
                        servers += 1
                        if "enabled: true" in "\n".join(lines[i:i+5]):
                            enabled += 1
                if l and not l.startswith((" ", "\t")) and l.strip() != "mcp_servers:":
                    in_mcp = False
        r = ("Connections", "GREEN" if servers and enabled == servers else "YELLOW", f"{enabled}/{servers} connected")
        _cache["mcp"] = r; _cache["mcp_ts"] = now
        return r
    except Exception:
        return ("Connections", "YELLOW", "config?")

def _check_cron():
    """Scheduled count alone lies — hermes's cron ticker is a thread that can
    die while chat still answers. Read its own liveness heartbeat
    (~/.hermes/cron/ticker_last_success) and go YELLOW when it's stale."""
    global _cache
    now = time.time()
    if _cache["cron"] and now - _cache["cron_ts"] < 60:
        return _cache["cron"]
    try:
        r = subprocess.run(["bash", "-lc", "hermes cron list"], capture_output=True, text=True, timeout=8)
        n = sum(1 for l in r.stdout.split("\n") if "[active]" in l)
        hb = os.path.join(HOME, ".hermes", "cron", "ticker_last_success")
        stale = os.path.exists(hb) and (now - os.path.getmtime(hb)) > 7200
        if n and stale:
            rv = ("Routines", "YELLOW", f"{n} scheduled, but none have run in a while")
        else:
            rv = ("Routines", "GREEN" if n else "YELLOW", f"{n} scheduled")
        _cache["cron"] = rv; _cache["cron_ts"] = now
        return rv
    except Exception:
        return ("Routines", "YELLOW", "?")

def _check_disk():
    try:
        u = shutil.disk_usage(HOME)
        pct = int(u.free / u.total * 100)
        return ("Disk space", "GREEN" if pct > 10 else "YELLOW", f"{pct}% free")
    except Exception:
        return ("Disk space", "YELLOW", "?")

def _check_custom():
    """Pro: owner-defined checks — argv command printing a JSON list of
    {label, status, detail}. Contract failures degrade to one YELLOW row."""
    try:
        r = subprocess.run(CHECKS_CMD, capture_output=True, text=True, timeout=10)
        rows = json.loads(r.stdout)
        return [(c["label"], c.get("status", "YELLOW"), c.get("detail", "")) for c in rows][:12]
    except Exception as e:
        return [("Custom checks", "YELLOW", str(e)[:60])]

def get_checks():
    """Run all checks in parallel."""
    checks = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(f) for f in (_check_chat, _check_mcp, _check_cron, _check_disk)]
        if CHECKS_CMD:
            futs.append(ex.submit(_check_custom))
        for f in as_completed(futs, timeout=14):
            try:
                r = f.result()
                checks.extend(r) if isinstance(r, list) else checks.append(r)
            except Exception: pass
    return checks

# ---- HTTP Server ----

class Handler(BaseHTTPRequestHandler):
    def _json(self, d, s=200):
        self.send_response(s); self.send_header("Content-Type","application/json"); self.send_header("Access-Control-Allow-Origin","*"); self.end_headers()
        self.wfile.write(json.dumps(d, ensure_ascii=False).encode())
    def _html(self, c, s=200):
        self.send_response(s); self.send_header("Content-Type","text/html; charset=utf-8"); self.end_headers()
        self.wfile.write(c.encode())

    def _check_auth(self):
        """HTTP Basic Auth. Returns True if authorized."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Sasha"')
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Authorization required")
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            user, pwd = decoded.split(":", 1)
            stored = open(AUTH_FILE).read().strip()
            expected_user, expected_pwd = stored.split(":", 1)
            import hmac
            if (hmac.compare_digest(user.encode(), expected_user.encode())
                    and hmac.compare_digest(pwd.encode(), expected_pwd.encode())):
                return True
        except: pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Sasha"')
        self.end_headers()
        self.wfile.write(b"Invalid credentials")
        return False

    def _serve_static(self, path):
        """Serve static files from doc directories under the same auth domain."""
        # Map URL paths to filesystem dirs (mirrors nginx volume mounts)
        # from config: "static_dirs": {"/weather": "~/some/docs", ...}
        root_map = {p: os.path.expanduser(d) for p, d in CONFIG.get("static_dirs", {}).items()}
        # Find which root this path maps to
        matched_root = None
        rel = path
        for prefix, root in root_map.items():
            if path == prefix or path.startswith(prefix + "/"):
                matched_root = root
                rel = path[len(prefix)+1:] if len(path) > len(prefix) else ""
                break
        if not matched_root:
            self._json({"error":"not found"},404)
            return

        full = os.path.join(matched_root, rel) if rel else matched_root
        full = os.path.normpath(full)
        # Security: prevent escaping the root
        if not full.startswith(os.path.normpath(matched_root)):
            self._json({"error":"bad path"},403)
            return

        if os.path.isdir(full):
            # Serve index.html if exists, else directory listing
            idx = os.path.join(full, "index.html")
            if os.path.isfile(idx):
                full = idx
            else:
                self._html_dir(full, path)
                return

        if os.path.isfile(full):
            self._send_file(full)
        else:
            self._json({"error":"not found"},404)

    def _html_dir(self, dirpath, url_prefix):
        """Simple directory listing."""
        try:
            entries = sorted(os.listdir(dirpath))
        except: entries = []
        links = []
        for e in entries:
            if e.startswith("."): continue
            href = url_prefix.rstrip("/") + "/" + e
            is_dir = os.path.isdir(os.path.join(dirpath, e)) or (e.count(".") == 0 and not e.endswith(".html") and not e.endswith(".md"))
            disp = e + ("/" if is_dir else "")
            links.append(f'<a href="{href}">{disp}</a>')
        html = f"<!DOCTYPE html><html><head><meta charset=utf-8><title>Docs</title>" \
               f"<style>body{{font-family:system-ui,sans-serif;background:#0d1117;color:#c9d1d9;padding:20px}}" \
               f"a{{color:#58a6ff;display:block;padding:4px 0}}a:hover{{color:#fff}}</style></head>" \
               f"<body><h2 style=color:#8b949e>Index of {url_prefix}</h2>" \
               f"{''.join(links)}</body></html>"
        self._html(html)

    def _send_file(self, path):
        """Send a file with appropriate content type."""
        ext = os.path.splitext(path)[1].lower()
        types = {
            ".html": "text/html; charset=utf-8",
            ".htm": "text/html; charset=utf-8",
            ".css": "text/css",
            ".js": "application/javascript",
            ".json": "application/json",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
            ".md": "text/markdown; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
            ".woff2": "font/woff2",
        }
        ctype = types.get(ext, "application/octet-stream")
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json({"error":str(e)},500)

    def _proxy_term(self):
        """Transparently reverse-proxy /term/<id>/... to a loopback ttyd, under
        this dashboard's auth. One raw-socket tunnel serves both the HTTP asset
        fetches and the WebSocket upgrade — protocol-agnostic once bytes flow."""
        parts = urlparse(self.path).path.split("/", 3)
        tid = parts[2] if len(parts) > 2 else ""
        # Only configured terminals — never proxy to arbitrary loopback ports.
        port = TERM_PORTS.get(tid)
        if port is None:
            self._json({"error": "unknown terminal"}, 404)
            return
        rest = parts[3] if len(parts) > 3 else ""
        if rest == "":  # the iframe page mount itself, not assets/ws
            track("term:" + tid, self.client_address[0])
        try:
            backend = socket.create_connection(("127.0.0.1", port), timeout=5)
        except OSError as e:
            track("err:offline:" + tid, self.client_address[0])
            self._json({"error": f"terminal offline: {e}"}, 502)
            return
        try:
            # A WebSocket upgrade needs its persistent Connection: Upgrade tunnel,
            # pinned 1:1 to this backend. Every OTHER request is a plain HTTP asset
            # fetch — force Connection: close so the browser can NOT reuse this
            # client TCP connection for a different /term/<id>. This tunnel pins a
            # whole keep-alive connection to ONE backend (chosen by the first
            # request), so a reused connection would route e.g. /term/hermes/ to
            # bash's ttyd (base -b /term/bash) which 404s any foreign path.
            is_ws = self.headers.get("Upgrade", "").lower() == "websocket"
            head = f"{self.command} {self.path} {self.request_version}\r\n"
            for k, v in self.headers.items():
                if not is_ws and k.lower() in ("connection", "keep-alive"):
                    continue
                head += f"{k}: {v}\r\n"
            if not is_ws:
                head += "Connection: close\r\n"
            head += "\r\n"
            backend.sendall(head.encode("latin-1"))
            self._pump(self.connection, backend)
        finally:
            try: backend.close()
            except OSError: pass
            try: self.connection.setblocking(True)
            except OSError: pass
            self.close_connection = True

    def _proxy_gw(self):
        """Reverse-proxy /gw/* to the hermes gateway (`hermes serve`, loopback),
        under this dashboard's auth. The gateway's DNS-rebinding guard only
        accepts loopback Host values, and its websocket origin check is
        localhost-only — so both Host and Origin are rewritten. Same tunnel
        rules as _proxy_term: ws upgrades keep their pinned connection,
        everything else gets Connection: close."""
        try:
            backend = socket.create_connection(("127.0.0.1", GW_PORT), timeout=5)
        except OSError as e:
            self._json({"error": f"gateway offline: {e}"}, 502)
            return
        try:
            fwd = self.path[len("/gw"):] or "/"
            is_ws = self.headers.get("Upgrade", "").lower() == "websocket"
            head = f"{self.command} {fwd} {self.request_version}\r\n"
            for k, v in self.headers.items():
                kl = k.lower()
                if kl in ("host", "origin"):
                    continue
                if not is_ws and kl in ("connection", "keep-alive"):
                    continue
                head += f"{k}: {v}\r\n"
            head += f"Host: 127.0.0.1:{GW_PORT}\r\n"
            head += f"Origin: http://127.0.0.1:{GW_PORT}\r\n"
            if not is_ws:
                head += "Connection: close\r\n"
            head += "\r\n"
            backend.sendall(head.encode("latin-1"))
            self._pump(self.connection, backend)
        finally:
            try: backend.close()
            except OSError: pass
            try: self.connection.setblocking(True)
            except OSError: pass
            self.close_connection = True

    @staticmethod
    def _pump(a, b):
        """Bidirectional tunnel between two sockets — one thread per direction,
        blocking mode. Blocking sends apply backpressure: a full kernel send
        buffer makes sendall WAIT instead of raising EAGAIN. The old non-blocking
        select-loop treated a send-side EAGAIN (BlockingIOError, i.e. OSError) as
        fatal — harmless over loopback (huge buffers) but it tore live terminals
        down for real LAN clients (small buffers) on the first tmux redraw burst,
        leaving a connected-but-blank 'black box' terminal."""
        a.setblocking(True); b.setblocking(True)

        def one_way(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                for s in (src, dst):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        t = threading.Thread(target=one_way, args=(b, a), daemon=True)
        t.start()
        one_way(a, b)
        t.join(timeout=5)

    def do_GET(self):
        p = urlparse(self.path).path
        # Owner-designated public static prefixes (report pages other tools
        # link to) skip auth; everything else authenticates first.
        if PUBLIC_PATHS and p.startswith(PUBLIC_PATHS):
            return self._serve_static(p)
        if not self._check_auth(): return
        if p.startswith("/term/"):
            return self._proxy_term()
        if p == "/gw" or p.startswith("/gw/"):
            return self._proxy_gw()
        if p == "/":
            track("load", self.client_address[0])
            self._html(HTML)  # skills rendered inline in HTML
        elif p == "/api/t":
            q = urlparse(self.path).query
            if q.startswith("e="): track(unquote(q[2:]), self.client_address[0])
            self._json({"ok": 1})
        elif p == "/api/usage":
            self._json(usage_summary())
        elif p == "/api/status":
            try: self._json({"checks":[{"label":l,"status":s,"detail":d} for l,s,d in get_checks()]})
            except Exception as e: self._json({"error":str(e)})
        # /api/exec removed 2026-07-04 — the redesigned UI spawns no terminals
        # client-side, so an authed arbitrary-shell endpoint has no reason to exist.
        elif p == "/api/search":
            q = urlparse(self.path).query
            if q.startswith("q="):
                q = unquote(q[2:])
                if not SEARCH_CMD:
                    self._json({"error": "search isn't set up yet"}); return
                try:
                    r = subprocess.run(SEARCH_CMD + [q], capture_output=True, text=True, timeout=30)
                except FileNotFoundError:
                    self._json({"error": "search isn't wired up right (command not found) — tell whoever set me up"}); return
                except subprocess.TimeoutExpired:
                    self._json({"error": "search took too long — try again in a moment"}); return
                try: self._json(json.loads(r.stdout))
                except: self._json({"error":(r.stderr or r.stdout or "?").strip()[:200]})
            else: self._json({"error":"no query"})
        elif p == "/api/all":
            try:
                checks = [{"label":l,"status":s,"detail":d} for l,s,d in get_checks()]
                notes = []
                try:
                    if NOTES_DIR and os.path.isdir(NOTES_DIR):
                        for f in sorted(os.listdir(NOTES_DIR), reverse=True)[:15]:
                            if not f.endswith(".md"): continue
                            c = open(os.path.join(NOTES_DIR, f)).read()[:200]
                            notes.append({"file": f, "preview": c[:150]})
                except OSError: pass
                evs = []
                if EVENTS_CMD:
                    try:
                        r = subprocess.run(EVENTS_CMD, capture_output=True, text=True, timeout=10)
                        evs = json.loads(r.stdout).get("events", [])[-30:]
                    except Exception: pass
                self._json({"checks":checks,"events":evs,"notes":notes})
            except Exception as e: self._json({"error":str(e)})
        elif p == "/api/read":
            q = urlparse(self.path).query
            if q.startswith("path="):
                fp = unquote(q[5:])
                # Handle relative paths by trying known base directories
                if not fp.startswith("/"):
                    for b in READ_DIRS:
                        candidate = os.path.join(b, fp)
                        if os.path.isfile(candidate):
                            fp = candidate
                            break
                fp = os.path.realpath(fp)
                ok = any(fp == os.path.realpath(a) or fp.startswith(os.path.realpath(a) + os.sep) for a in READ_DIRS)
                if ok and os.path.isfile(fp):
                    try:
                        c = open(fp, encoding="utf-8", errors="replace").read()
                        self._json({"path":fp,"content":c[:3000]})
                    except Exception as e:
                        self._json({"error":str(e)})
                else: self._json({"error":"path not allowed or not found"})
            else: self._json({"error":"no path"})
        else:
            # Try serving as static file (docs, reports, weather, etc.)
            self._serve_static(p)

    def do_POST(self):
        if not self._check_auth(): return
        p = urlparse(self.path).path
        if p.startswith("/term/"):
            return self._proxy_term()
        if p == "/gw" or p.startswith("/gw/"):
            return self._proxy_gw()
        if p.startswith("/api/run/"):
            aid = p[len("/api/run/"):]
            act = ACTIONS.get(aid)
            if not act or not act.get("cmd"):
                self._json({"error": "that action isn't set up"}, 404); return
            try:
                r = subprocess.run(act["cmd"], capture_output=True, text=True, timeout=180)
                self._json({"output": (r.stdout or r.stderr or "done").strip()[:2000]})
            except subprocess.TimeoutExpired:
                self._json({"output": "That took too long \u2014 try again in a bit."})
            except FileNotFoundError:
                self._json({"output": "That action isn't wired up right (command not found) \u2014 tell whoever set me up."})
        else: self._json({"error":"not found"},404)

    def log_message(self, f, *a):
        if "/api/" in str(a): print("[%s] %s" % (self.log_date_time_string(), f % a), file=sys.stderr)


# ---- Server-rendered pieces (from config) ----
SERVICES_HTML = "".join(
    f'<a class="sc-card" href="{s["href"]}" target="_blank"><span class="sc-ico">{s.get("icon","&#128279;")}</span>'
    f'<div class="sc-body"><div class="sc-t">{s["title"]}</div><div class="sc-d">{s.get("desc","")}</div></div>'
    f'<span class="sc-ext">&#8599;</span></a>'
    for s in SERVICES) or '<div style="color:var(--faint);font-size:12px">Nothing linked yet.</div>'

def _build_chips():
    chips, coach = [], {}
    first = True
    for aid, a in ACTIONS.items():
        if not a.get("chip"): continue
        cls = "chip go" if first else "chip"
        first = False
        chips.append('<button class="%s" onclick="chipRun(&quot;%s&quot;)"><span class="ci">%s</span>%s</button>'
                     % (cls, aid, a.get("emoji", "&#9889;"), a["chip"]))
    if SEARCH_CMD:
        chips.append('<button class="chip" onclick="chipFind()"><span class="ci">&#128269;</span>Find a note</button>')
    if WEATHER_URL:
        chips.append('<a class="chip" href="%s" target="_blank" onclick="trk(&quot;card:weather&quot;)"><span class="ci">&#127780;&#65039;</span>Weather</a>' % WEATHER_URL)
    for c in COACH_CHIPS:
        chips.append('<button class="chip" onclick="chipCoach(&quot;%s&quot;)"><span class="ci">%s</span>%s</button>'
                     % (c["id"], c.get("emoji", "&#128172;"), c["chip"]))
        coach[c["id"]] = c["text"]
    return "".join(chips), coach

CHIPS_HTML, COACH_MAP = _build_chips()
NICE_MAP = {aid: [a.get("label", aid), a.get("busy", "Working\u2026")] for aid, a in ACTIONS.items()}

# ---- Pro: skills sidebar (auto-discovered) + hero tabs ----
def _build_skills():
    if not (IS_PRO and SKILLS_DIR and os.path.isdir(SKILLS_DIR)):
        return ""
    names = sorted(d for d in os.listdir(SKILLS_DIR)
                   if os.path.isdir(os.path.join(SKILLS_DIR, d)) and not d.startswith("."))
    if not names:
        return ""
    items = "".join(f'<div class="sk" onclick="useSkill(&quot;{n}&quot;)">{n}</div>' for n in names)
    return (f'<aside id="side"><h2>Skills</h2><div id="sk">{items}</div>'
            f'<div class="sk-hint">Click one &mdash; it starts in the chat input.</div></aside>')

SKILLS_HTML = _build_skills()

def _build_hero_tabs():
    """Pro: the hero grows tabs \u2014 Chat (native) plus each configured terminal."""
    extra = [t for t in TERM_PORTS if not (CHAT_MODE != "ws" and t == "hermes")]
    if not (IS_PRO and CHAT_MODE == "ws" and extra):
        return ""
    tabs = ['<span class="ht ht-a" data-h="chat" onclick="heroTab(&quot;chat&quot;)">Chat</span>']
    tabs += [f'<span class="ht" data-h="{t}" onclick="heroTab(&quot;{t}&quot;)">{t}</span>' for t in extra]
    return '<div id="htabs">' + "".join(tabs) + '</div>'

HERO_TABS = _build_hero_tabs()

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sasha</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#127797;</text></svg>">
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,550;1,9..144,450&family=Atkinson+Hyperlegible:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
<style>
 :root{
   /* Palette: "high-desert morning" — warm paper ground, ink brown, one clay
      action color, sage for good news. Chosen for warmth + legibility. */
   --dusk:#F6F1E7; --adobe:#FDFAF4; --raise:#F3EBDC;
   --moon:#33291F; --sand:#4A3F32; --quail:#7A6E5F; --faint:#9C8F7D;
   --horizon:#B4552D; --ember:#9C4A26; --sage:#5C7A52; --sage-bg:rgba(111,143,102,.16);
   --warn:#9A7014; --warn-bg:rgba(154,112,20,.14); --bad:#B3392E; --bad-bg:rgba(179,57,46,.12);
   --line:rgba(51,41,31,.16); --line-s:rgba(51,41,31,.09);
   --sky:linear-gradient(90deg,#C97E3E 0%,#B4552D 45%,#6F8F66 100%);
   --font:'Atkinson Hyperlegible',system-ui,-apple-system,'Segoe UI',sans-serif;
   --disp:'Fraunces',Georgia,serif;
 }
 *{margin:0;padding:0;box-sizing:border-box}
 html,body{height:100%}
 body{font-family:var(--font);background:var(--dusk);color:var(--sand);font-size:16px;line-height:1.55;
   display:flex;flex-direction:row;overflow:hidden;-webkit-font-smoothing:antialiased}
 #wrap{width:100%;max-width:1020px;margin:0 auto;padding:12px 22px 10px;display:flex;flex-direction:column;flex:1;min-height:0}
 ::-webkit-scrollbar{width:6px}::-webkit-scrollbar-track{background:transparent}
 ::-webkit-scrollbar-thumb{background:rgba(51,41,31,.18);border-radius:4px}
 button{font-family:var(--font)}
 :focus-visible{outline:2px solid var(--horizon);outline-offset:2px;border-radius:6px}

 /* ── Greeting ── */
 #hero{flex:none}
 #greet-row{display:flex;align-items:flex-end;justify-content:space-between;gap:14px;flex-wrap:wrap}
 .eyebrow{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--quail);font-weight:700}
 h1{font-family:var(--disp);font-weight:550;font-size:clamp(19px,2.6vw,26px);color:var(--moon);
    letter-spacing:-.01em;line-height:1.15;margin-top:1px}
 #sub{color:var(--quail);font-size:13px;margin-top:1px}
 #pulse{flex:none;display:inline-flex;align-items:center;gap:8px;font-size:14px;font-weight:700;
   padding:8px 16px;border-radius:999px;border:1px solid var(--line);background:var(--adobe);
   color:var(--sage);cursor:pointer;transition:border-color .15s}
 #pulse:hover{border-color:rgba(246,239,231,.2)}
 #pulse.p-warn{color:var(--warn)} #pulse.p-bad{color:var(--bad)}
 #horizon{height:3px;border-radius:999px;background:var(--sky);margin:10px 0 10px;opacity:.9}

 /* ── Action chips ── */
 #chips{display:flex;gap:10px;flex-wrap:wrap;flex:none}
 .chip{display:inline-flex;align-items:center;gap:8px;padding:8px 15px;border-radius:999px;
   background:var(--adobe);border:1px solid var(--line);color:var(--moon);font-size:14px;font-weight:700;
   cursor:pointer;transition:transform .12s,border-color .12s,background .12s;user-select:none}
 .chip:hover{background:var(--raise);border-color:var(--horizon);transform:translateY(-1px)}
 .chip:active{transform:translateY(0)}
 .chip .ci{font-size:17px}
 .chip.go{background:var(--horizon);border-color:var(--horizon);color:#FDF8EE}
 .chip.go:hover{background:var(--ember);border-color:var(--ember)}

 /* ── Coach mark ── */
 #coach{display:none;align-items:center;gap:10px;margin-top:12px;padding:12px 16px;border-radius:12px;
   background:var(--adobe);border:1px dashed rgba(180,85,45,.5);color:var(--sand);font-size:15px}
 #coach b{color:var(--moon);font-weight:700}
 #coach q{font-style:italic;color:var(--horizon);quotes:'\201C' '\201D'}
 #coach .cx{margin-left:auto;color:var(--faint);cursor:pointer;font-size:18px;line-height:1;padding:0 4px}
 #coach .cx:hover{color:var(--moon)}

 /* ── Find row + results ── */
 #findrow{display:none;gap:10px;margin-top:12px}
 #sr-q{flex:1;background:var(--adobe);border:1px solid var(--line);border-radius:12px;color:var(--moon);
   padding:12px 16px;font-size:16px;outline:none;font-family:var(--font)}
 #sr-q:focus{border-color:var(--horizon)}
 #sr-q::placeholder{color:var(--faint)}
 #sr-btn{background:var(--horizon);border:none;border-radius:12px;color:#FDF8EE;padding:12px 22px;
   font-size:15px;font-weight:700;cursor:pointer}
 #sr-btn:hover{background:var(--ember)}
 #sr-info{color:var(--quail);font-size:13px;padding:8px 2px 0}
 #sr-results{overflow-y:auto;max-height:34vh;margin-top:6px}
 .sr-r{background:var(--adobe);border:1px solid var(--line);border-radius:12px;padding:12px 15px;
   margin-bottom:8px;cursor:pointer;transition:border-color .12s}
 .sr-r:hover{border-color:var(--horizon)}
 .sr-h{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
 .sr-k{color:var(--horizon);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em}
 .sr-p{color:var(--faint);font-size:11px;margin-left:auto;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .sr-s{display:none}
 .sr-t{color:var(--sand);font-size:14px;margin-top:5px;line-height:1.5}
 .sr-e{color:var(--bad);font-size:14px;padding:10px 0}
 .sr-none{color:var(--faint);font-size:14px;padding:16px 0;text-align:center}

 /* ── Run-output ── */
 #ro{display:none;align-items:center;gap:10px;margin-top:12px;padding:12px 16px;border-radius:12px;
   background:var(--adobe);border:1px solid var(--line);font-size:15px}
 #ro-l{font-weight:700;color:var(--horizon);white-space:nowrap}
 #ro-t{color:var(--sand);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 #ro-x{color:var(--faint);cursor:pointer;font-size:19px;padding:0 5px;line-height:1}
 #ro-x:hover{color:var(--moon)}
 #ro-results{margin-top:8px;max-height:30vh;overflow-y:auto}
 .rr-hide{display:none}
 .rr-card{display:flex;gap:11px;align-items:flex-start;padding:11px 14px;margin-bottom:6px;
   background:var(--adobe);border:1px solid var(--line);border-radius:12px}
 .rr-badge{font-size:10px;font-weight:700;padding:3px 10px;border-radius:999px;white-space:nowrap;
   flex:none;margin-top:2px;text-transform:uppercase;letter-spacing:.05em}
 .rr-urgent{background:var(--bad-bg);color:var(--bad)}
 .rr-fyi{background:var(--warn-bg);color:var(--warn)}
 .rr-noise{background:rgba(51,41,31,.07);color:var(--faint)}
 .rr-body{flex:1;min-width:0}
 .rr-subj{font-weight:700;font-size:14px;color:var(--moon);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .rr-subj .rr-src{font-weight:400;font-size:11px;color:var(--faint);margin-left:5px}
 .rr-from{font-size:12px;color:var(--quail);margin-top:1px}
 .rr-snip{font-size:12px;color:var(--faint);margin-top:2px;line-height:1.45;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

 /* ── Chat (the hero) ── */
 #chat{flex:1;min-height:180px;display:flex;flex-direction:column;margin-top:12px;border-radius:14px;
   border:1px solid var(--line);background:#F1EADA;overflow:hidden}
 #chat iframe{flex:1;width:100%;border:none}
 /* native ws chat */
 #cwrap{flex:1;display:flex;flex-direction:column;min-height:0}
 #cmsgs{flex:1;overflow-y:auto;padding:16px 16px 6px;display:flex;flex-direction:column;gap:10px}
 .cb{max-width:76%;padding:10px 14px;border-radius:14px;font-size:15px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
 .cb-u{align-self:flex-end;background:var(--horizon);color:#FDF8EE;border-bottom-right-radius:4px}
 .cb-a{align-self:flex-start;background:var(--adobe);color:var(--moon);border:1px solid var(--line);border-bottom-left-radius:4px}
 .cb-e{align-self:center;background:var(--bad-bg);color:var(--bad);font-size:13px;border-radius:10px}
 .cb-note{align-self:center;color:var(--faint);font-size:12px}
 #cstatus{min-height:20px;padding:0 18px 4px;color:var(--quail);font-size:13px;font-style:italic}
 #crow{display:flex;gap:8px;padding:8px 10px 10px;border-top:1px solid var(--line);background:var(--adobe)}
 #cin{flex:1;background:#FDFAF4;border:1px solid var(--line);border-radius:12px;color:var(--moon);
   padding:11px 14px;font-size:16px;outline:none;font-family:var(--font);resize:none;max-height:120px}
 #cin:focus{border-color:var(--horizon)}
 #cin::placeholder{color:var(--faint)}
 #csend{background:var(--horizon);border:none;border-radius:12px;color:#FDF8EE;padding:0 22px;
   font-size:15px;font-weight:700;cursor:pointer}
 #csend:hover{background:var(--ember)}
 #csend:disabled{opacity:.5;cursor:default}
 .ap-card{align-self:flex-start;background:var(--warn-bg);border:1px dashed var(--warn);border-radius:12px;
   padding:12px 14px;font-size:14px;color:var(--moon);max-width:76%}
 .ap-card button{margin:8px 8px 0 0;padding:7px 14px;border-radius:999px;border:1px solid var(--line);
   background:var(--adobe);color:var(--moon);font-weight:700;cursor:pointer;font-family:var(--font)}
 .ap-card button.yes{background:var(--sage);border-color:var(--sage);color:#1e2a1a}

 /* ── Pro: skills sidebar + hero tabs + terminal tab ── */
 #side{width:190px;min-width:190px;background:var(--adobe);border-right:1px solid var(--line);
   overflow-y:auto;padding:14px 10px;display:flex;flex-direction:column}
 #side h2{font-size:11px;letter-spacing:.13em;text-transform:uppercase;color:var(--quail);
   font-weight:700;padding:0 6px 8px}
 .sk{padding:6px 10px;font-size:13px;font-weight:700;color:var(--sand);border-radius:8px;
   cursor:pointer;margin-bottom:1px}
 .sk:hover{background:var(--raise);color:var(--moon)}
 .sk-hint{margin-top:auto;padding:10px 6px 0;font-size:11px;color:var(--faint);line-height:1.4}
 #htabs{display:flex;gap:2px;background:var(--adobe);border-bottom:1px solid var(--line);padding:4px 8px 0}
 .ht{padding:6px 16px;font-size:13px;font-weight:700;color:var(--quail);cursor:pointer;
   border-radius:8px 8px 0 0;border-bottom:2px solid transparent}
 .ht:hover{color:var(--moon)}
 .ht-a{color:var(--moon);border-bottom-color:var(--horizon)}
 #hterm{flex:1;min-height:0}
 #hterm iframe{width:100%;height:100%;border:none}
 @media (max-width:900px){ #side{display:none} }

 /* ── Footer + drawer ── */
 #foot{flex:none;display:flex;align-items:center;gap:10px;padding:10px 2px 0}
 #hood-t{background:none;border:none;color:var(--faint);font-size:13px;cursor:pointer;padding:4px 6px}
 #hood-t:hover{color:var(--sand)}
 #lr{margin-left:auto;font-size:12px;color:var(--faint)}
 #hood{display:none;overflow-y:auto;max-height:42vh;margin-top:8px;padding:14px;border-radius:14px;
   background:var(--adobe);border:1px solid var(--line)}
 #hood h3{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--quail);margin:14px 0 8px}
 #hood h3:first-child{margin-top:0}
 .sg{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:8px}
 .sc{background:var(--dusk);border:1px solid var(--line-s);border-radius:10px;padding:10px 12px}
 .sch{display:flex;justify-content:space-between;align-items:center}
 .scl{font-weight:700;font-size:13px;color:var(--moon)}
 .b{padding:2px 10px;border-radius:999px;font-size:10px;font-weight:700;letter-spacing:.03em}
 .b-green{background:var(--sage-bg);color:var(--sage)}
 .b-yellow{background:var(--warn-bg);color:var(--warn)}
 .b-red{background:var(--bad-bg);color:var(--bad)}
 .scd{font-size:11px;color:var(--quail);margin-top:3px;line-height:1.4}
 .sc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px}
 .sc-card{display:flex;gap:10px;align-items:flex-start;text-decoration:none;color:var(--sand);
   background:var(--dusk);border:1px solid var(--line-s);border-radius:12px;padding:10px 12px;transition:border-color .12s}
 .sc-card:hover{border-color:var(--horizon)}
 .sc-ico{font-size:16px;flex:none;width:26px;height:26px;display:grid;place-items:center;background:var(--adobe);border:1px solid var(--line-s);border-radius:8px}
 .sc-body{min-width:0;flex:1}
 .sc-t{font-weight:700;font-size:13px;color:var(--moon)}
 .sc-d{color:var(--quail);font-size:11px;margin-top:3px;line-height:1.4}
 .sc-ext{color:var(--faint);font-size:11px;margin-left:auto}
 .et{width:100%;font-size:12px;border-collapse:collapse}
 .et td{padding:5px 8px;border-bottom:1px solid var(--line-s)}
 .ets{color:var(--horizon);font-weight:700;font-size:11px}
 .ett{color:var(--quail);font-size:11px}
 .etd{color:var(--faint);font-size:11px}
 .an{background:var(--dusk);border:1px solid var(--line-s);border-radius:10px;padding:8px 12px;margin-bottom:5px;font-size:12px}
 .ant{font-weight:700;color:var(--moon)}
 .anp{color:var(--quail);font-size:11px;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;line-height:1.4}

 @media (prefers-reduced-motion:no-preference){
   #hero,#chips,#chat{animation:rise .5s ease both}
   #chips{animation-delay:.08s} #chat{animation-delay:.16s}
   @keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
 }
 @media (max-width:640px){
   #wrap{padding:16px 14px 10px}
   #greet-row{align-items:flex-start}
   #pulse{font-size:13px;padding:7px 13px}
   .chip{font-size:14px;padding:10px 15px}
 }
</style>
</head>
<body>
__SKILLS__
<div id="wrap">
 <header id="hero">
  <div id="greet-row">
   <div>
    <div class="eyebrow" id="dateline">__PLACE__</div>
    <h1 id="greet">Hello, __NAME__</h1>
    <div id="sub">I&rsquo;m Sasha &mdash; type anything in the chat below, or start with one of these.</div>
   </div>
   <button id="pulse" onclick="toggleHood(true)" title="How things are running">&#9679; Checking&hellip;</button>
  </div>
  <div id="horizon"></div>
 </header>

 <div id="chips">__CHIPS__</div>

 <div id="coach"><span id="coach-t"></span><span class="cx" onclick="hideCoach()">&times;</span></div>

 <div id="findrow">
  <input id="sr-q" type="text" placeholder="Search our notes, decisions, and everything written down&hellip;" onkeydown="if(event.key==='Enter')doSearch()">
  <button id="sr-btn" onclick="doSearch()">Find</button>
 </div>
 <div id="sr-info"></div>
 <div id="sr-results"></div>

 <div id="ro"><span id="ro-l"></span><span id="ro-t"></span><span id="ro-x" onclick="hideRo()">&times;</span></div>
 <div id="ro-results" class="rr-hide"></div>

 <main id="chat">__HERO_TABS____CHAT_HERO__<div id="hterm" style="display:none"></div></main>

 <div id="foot">
  <button id="hood-t" onclick="toggleHood()">Under the hood</button>
  <span id="lr"></span>
 </div>
 <div id="hood">
  <h3>How things are running</h3><div class="sg" id="checks"></div>
  <h3>More pages</h3><div class="sc-grid" id="svc">""" + SERVICES_HTML + """</div>
  <h3>Recent activity</h3><table class="et" id="etab"></table>
  <h3>Notes the AI left</h3><div id="atab"></div>
 </div>
</div>
<script>
// INT-4 telemetry beacon — event names only, fire-and-forget
function trk(e){try{fetch(new URL('/api/t?e='+encodeURIComponent(e),location.origin).href)}catch(_){}}

// Greeting — time-aware, plain words
(function(){
  const now=new Date(),h=now.getHours();
  const part=h<12?'Good morning':h<17?'Good afternoon':'Good evening';
  document.getElementById('greet').textContent=part+', __NAME__';
  document.getElementById('dateline').textContent='__PLACE__ \u00b7 '+now.toLocaleDateString('en-US',{weekday:'long',month:'long',day:'numeric'});
})();

// Chips
function chipRun(id){trk('card:'+id);hideCoach();run(id)}
function chipFind(){
  trk('card:find');hideCoach();
  const f=document.getElementById('findrow');
  const open=f.style.display==='flex';
  f.style.display=open?'none':'flex';
  if(!open)setTimeout(()=>document.getElementById('sr-q').focus(),50);
  else{document.getElementById('sr-info').textContent='';document.getElementById('sr-results').innerHTML=''}
}
const COACH=__COACH__;
function chipCoach(k){trk('card:'+k);const c=document.getElementById('coach');document.getElementById('coach-t').innerHTML=COACH[k];c.style.display='flex'}
function hideCoach(){document.getElementById('coach').style.display='none'}
function hideRo(){document.getElementById('ro').style.display='none';document.getElementById('ro-results').style.display='none'}

// Under-the-hood drawer
function toggleHood(open){
  const hd=document.getElementById('hood');
  const show=open===true?true:hd.style.display!=='block';
  if(show)trk('hood:open');
  hd.style.display=show?'block':'none';
}

// Build absolute same-origin URLs — location.origin never carries user:pass,
// so these work even when the page was opened with credentials in the URL.
function u(p){return new URL(p,location.origin).href}
async function ap(p,m){const r=await fetch(u(p),{method:m||'GET'});return r.json()}

// Status — plain words up top, detail in the drawer
async function refreshAll(){
  const pu=document.getElementById('pulse');
  let d; try{d=await ap('/api/all')}catch(_){d={error:1}}
  if(d.error){pu.className='p-bad';pu.innerHTML='&#9679; Can&rsquo;t check right now';return}
  const o=d.checks||[]; const red=o.some(c=>c.status==='RED'); const yel=o.some(c=>c.status==='YELLOW');
  pu.className=red?'p-bad':yel?'p-warn':'';
  pu.innerHTML='&#9679; '+(red?'Something needs attention':yel?'Running &mdash; one thing to watch':'All&rsquo;s well');
  document.getElementById('checks').innerHTML=o.map(c=>`<div class="sc"><div class="sch"><span class="scl">${c.label}</span><span class="b b-${c.status.toLowerCase()}">${c.status}</span></div><div class="scd">${c.detail||''}</div></div>`).join('');
  const es=(d.events||[]).slice(-25);
  document.getElementById('etab').innerHTML=es.map(e=>`<tr><td class="ets">${e.source}</td><td class="ett">${e.type}</td><td class="etd">${(e.ts||'').slice(11,19)}</td></tr>`).join('');
  document.getElementById('atab').innerHTML=(d.notes||[]).slice(0,12).map(n=>`<div class="an"><div class="ant">${n.file}</div><div class="anp">${(n.preview||'').slice(0,120)}</div></div>`).join('');
  document.getElementById('lr').textContent='checked '+new Date().toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});
}

// Find
async function doSearch(){
  const q=document.getElementById('sr-q').value.trim(); if(!q)return;
  trk('search'); // that a search happened — never the query
  const info=document.getElementById('sr-info'),res=document.getElementById('sr-results');
  info.textContent='Looking\u2026'; res.innerHTML='';
  const d=await ap('/api/search?q='+encodeURIComponent(q));
  if(d.error){info.textContent='';res.innerHTML=`<div class="sr-e">That didn&rsquo;t work: ${d.error}</div>`;return}
  if(!d.results||d.results.length===0){info.textContent='';res.innerHTML='<div class="sr-none">Nothing found &mdash; try different words, or ask in the chat below.</div>';return}
  info.textContent=d.count+' found';
  res.innerHTML=d.results.map((r,i)=>`<div class="sr-r" onclick="expandResult(${i})"><div class="sr-h"><span class="sr-k">${r.kind}</span><span class="sr-p">${r.path.split('/').pop()}</span></div><div class="sr-t" id="sr-t-${i}">${esc(r.text)}</div><div class="sr-c" id="sr-c-${i}" style="display:none"></div><div class="sr-f" id="sr-f-${i}" style="display:none;font-size:12px;color:var(--faint);margin-top:3px">opening&hellip;</div></div>`).join('');
  window._sr = d.results;
}
async function expandResult(i){
  const c=document.getElementById('sr-c-'+i),f=document.getElementById('sr-f-'+i),t=document.getElementById('sr-t-'+i);
  if(c.style.display==='block'){c.style.display='none';f.style.display='none';t.style.display='block';return}
  if(c.textContent){c.style.display='block';f.style.display='none';t.style.display='none';return}
  f.style.display='block';
  const r=window._sr[i],d=await ap('/api/read?path='+encodeURIComponent(r.path));
  f.style.display='none';
  if(d.error){c.innerHTML=`<div class="sr-e">Couldn&rsquo;t open it: ${d.error}</div>`;c.style.display='block';t.style.display='none';return}
  c.innerHTML=`<pre style="font-size:13px;color:var(--sand);line-height:1.5;margin-top:4px;overflow-x:auto;white-space:pre-wrap;font-family:var(--font)">${esc(d.content)}</pre>`;
  c.style.display='block';t.style.display='none';
}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}

// Run an action (friendly labels; telemetry keeps the raw names)
const NICE=__NICE__;
async function run(n){
  trk('run:'+n);
  const l=document.getElementById('ro-l'),t=document.getElementById('ro-t'),r=document.getElementById('ro'),rr=document.getElementById('ro-results');
  const nice=NICE[n]||[n,'Working\u2026'];
  l.textContent=nice[0]; t.textContent=nice[1];
  r.style.display='flex';rr.style.display='none';rr.className='rr-hide';
  const d=await ap('/api/run/'+n,'POST');
  const out=d.output||d.error||'done';
  t.textContent=out.length>90?out.slice(0,87)+'\u2026':out;
  rr.innerHTML='';
  if(out&&out[0]==='{')try{
    const j=JSON.parse(out);
    if(j.emails){
      t.textContent='Here\u2019s what\u2019s in your inbox:';
      rr.innerHTML=j.emails.map(function(e){
        var b=e.triage==='URGENT'?'rr-urgent':e.triage==='FYI'?'rr-fyi':'rr-noise';
        var lab=e.triage==='URGENT'?'needs you':e.triage==='FYI'?'good to know':'skip it';
        var s=esc(e.source||'');
        return `<div class="rr-card"><span class="rr-badge ${b}">${lab}</span><div class="rr-body"><div class="rr-subj">${esc(e.subject)}<span class="rr-src">${s}</span></div><div class="rr-from">${esc(e.from)}</div><div class="rr-snip">${esc(e.snippet)}</div></div></div>`;
      }).join('');
      rr.className='';rr.style.display='block';
    }
  }catch(e){}
  setTimeout(refreshAll,3000);
}

// ── Native chat over hermes's /api/ws JSON-RPC gateway (CHAT_MODE "ws") ──
// NDJSON JSON-RPC both ways. Turn: prompt.submit -> message.start ->
// message.delta* -> message.complete. Approvals arrive as *.request events
// and are answered with the paired *.respond RPC.
const CHATMODE='__CHAT_MODE__';
let ws=null,rpcId=10,pend={},sid=null,curBubble=null,streaming=false,backoff=2;
function el(t,c,txt){const e=document.createElement(t);if(c)e.className=c;if(txt!==undefined)e.textContent=txt;return e}
function addMsg(cls,txt){const m=el('div','cb '+cls,txt);document.getElementById('cmsgs').appendChild(m);scrollC();return m}
function scrollC(){const b=document.getElementById('cmsgs');b.scrollTop=b.scrollHeight}
function setStatus(t){document.getElementById('cstatus').textContent=t||''}
function setSend(on){document.getElementById('csend').disabled=!on}
function initChat(){
  document.getElementById('csend').onclick=sendMsg;
  const cin=document.getElementById('cin');
  cin.addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMsg()}});
  connect();
}
async function connect(){
  setStatus('Connecting…');
  let tok='';
  try{const t=await (await fetch(u('/gw/'))).text();const m=t.match(/__HERMES_SESSION_TOKEN__="([^"]+)"/);if(m)tok=m[1]}catch(_){}
  if(!tok){ // gateway down or SPA unreadable — retry the fetch, don't hammer 403s
    setStatus('Waiting for the agent…');
    setTimeout(connect,backoff*1000);backoff=Math.min(backoff*2,30);return;
  }
  const proto=location.protocol==='https:'?'wss://':'ws://';
  try{ws=new WebSocket(proto+location.host+'/gw/api/ws?token='+encodeURIComponent(tok))}
  catch(_){setStatus('Reconnecting…');setTimeout(connect,backoff*1000);backoff=Math.min(backoff*2,30);return}
  ws.onopen=function(){backoff=2;setStatus('');attach()};
  ws.onmessage=function(ev){String(ev.data).split(String.fromCharCode(10)).forEach(function(line){
    line=line.trim();if(!line)return;
    let o;try{o=JSON.parse(line)}catch(_){return}
    handle(o);
  })};
  ws.onclose=function(){setStatus('Reconnecting…');streaming=false;setSend(true);setTimeout(connect,backoff*1000);backoff=Math.min(backoff*2,30)};
  ws.onerror=function(){try{ws.close()}catch(_){}};
}
function rpc(method,params){return new Promise(function(res,rej){const id=rpcId++;pend[id]={res:res,rej:rej};ws.send(JSON.stringify({jsonrpc:'2.0',id:id,method:method,params:params}))})}
async function attach(){
  const saved=localStorage.getItem('sashaSid')||'';
  try{
    let r=null;
    if(saved){try{r=await rpc('session.resume',{session_id:saved})}catch(_){r=null}}
    if(!r)r=await rpc('session.create',{});
    sid=r.session_id||saved;
    localStorage.setItem('sashaSid',sid);
    renderTranscript(r.messages||[]);
    if(!(r.messages||[]).length)addMsg('cb-note','Say hello — I answer in plain English.');
  }catch(e){setStatus('');addMsg('cb-e','I could not reach the agent just now — I will keep trying.')}
}
function renderTranscript(ms){
  const box=document.getElementById('cmsgs');box.innerHTML='';
  ms.forEach(function(m){
    const role=m.role||m.type||'';
    let txt=(typeof m.content==='string')?m.content:(m.text||'');
    if(!txt&&Array.isArray(m.content))txt=m.content.map(function(p){return p.text||''}).join('');
    if(!txt)return;
    if(role==='user')addMsg('cb-u',txt);
    else if(role==='assistant')addMsg('cb-a',txt);
  });
  scrollC();
}
async function sendMsg(){
  const cin=document.getElementById('cin');const txt=cin.value.trim();
  if(!txt||streaming||!ws||ws.readyState!==1||!sid)return;
  trk('chat:send');
  cin.value='';addMsg('cb-u',txt);streaming=true;setSend(false);curBubble=null;setStatus('Thinking…');
  try{await rpc('prompt.submit',{session_id:sid,text:txt})}
  catch(e){streaming=false;setSend(true);setStatus('');addMsg('cb-e','That did not go through — try again.')}
}
function handle(o){
  if(o.id!==undefined&&pend[o.id]){const p=pend[o.id];delete pend[o.id];if(o.error)p.rej(o.error);else p.res(o.result);return}
  if(o.method!=='event'||!o.params)return;
  const t=o.params.type,pl=o.params.payload||{};
  if(o.params.session_id&&sid&&o.params.session_id!==sid)return;
  if(t==='message.start'){curBubble=addMsg('cb-a','');setStatus('')}
  else if(t==='message.delta'){if(!curBubble)curBubble=addMsg('cb-a','');curBubble.textContent+=(pl.text||'');scrollC()}
  else if(t==='thinking.delta'){if(pl.text)setStatus(pl.text)}
  else if(t==='tool.start'){setStatus('Working on it…')}
  else if(t==='message.complete'){
    if(pl.text){if(!curBubble)curBubble=addMsg('cb-a','');curBubble.textContent=pl.text}
    curBubble=null;streaming=false;setSend(true);setStatus('');scrollC();
  }
  else if(t==='error'){addMsg('cb-e',pl.message||pl.text||'Something went wrong — try again.');streaming=false;setSend(true);setStatus('')}
  else if(t==='approval.request'||t==='clarify.request'||t==='sudo.request'||t==='secret.request'){renderAsk(t,o.params)}
}
function renderAsk(t,params){
  const pl=params.payload||{};
  const card=el('div','ap-card');
  card.appendChild(el('div','',pl.message||pl.prompt||pl.question||pl.text||'Sasha needs your OK for something.'));
  const kind=t.split('.')[0];
  const yes=el('button','yes','Yes, go ahead');const no=el('button','','No');
  yes.onclick=function(){answerAsk(kind,params,true);card.remove()};
  no.onclick=function(){answerAsk(kind,params,false);card.remove()};
  card.appendChild(yes);card.appendChild(no);
  document.getElementById('cmsgs').appendChild(card);scrollC();
}
function answerAsk(kind,params,ok){
  const pl=params.payload||{};
  const req={session_id:params.session_id||sid,approved:ok,response:ok?'yes':'no'};
  if(pl.request_id)req.request_id=pl.request_id;
  if(pl.id)req.id=pl.id;
  rpc(kind+'.respond',req).catch(function(){});
}
// Pro: hero tabs (Chat / terminals) + skills sidebar
function heroTab(t){
  trk('herotab:'+t);
  document.querySelectorAll('.ht').forEach(function(x){x.classList.toggle('ht-a',x.dataset.h===t)});
  const cw=document.getElementById('cwrap'),ht=document.getElementById('hterm');
  if(!cw||!ht)return;
  if(t==='chat'){ht.style.display='none';cw.style.display='flex';return}
  cw.style.display='none';ht.style.display='block';
  const want=u('/term/'+t+'/?fontSize=15');
  const cur=ht.firstElementChild;
  if(!cur||cur.dataset.t!==t){ht.innerHTML='';const f=document.createElement('iframe');f.dataset.t=t;f.src=want;ht.appendChild(f)}
}
function useSkill(n){
  trk('skill:'+n);
  heroTab('chat');
  const cin=document.getElementById('cin');
  if(cin){cin.value='/'+n+' ';cin.focus()}
}
if(CHATMODE==='ws')initChat();

refreshAll();setInterval(refreshAll,30000);
</script>
</body>
</html>"""

if CHAT_MODE == "ws":
    CHAT_HERO = ('<div id="cwrap"><div id="cmsgs"></div><div id="cstatus"></div>'
                 '<div id="crow"><textarea id="cin" rows="1" placeholder="Type here — plain words work"></textarea>'
                 '<button id="csend">Send</button></div></div>')
else:
    CHAT_HERO = '<iframe src="/term/hermes/?fontSize=16" title="Chat with Sasha"></iframe>'

HTML = (HTML.replace("__NAME__", NAME).replace("__PLACE__", PLACE)
            .replace("__CHIPS__", CHIPS_HTML)
            .replace("__COACH__", json.dumps(COACH_MAP))
            .replace("__NICE__", json.dumps(NICE_MAP))
            .replace("__CHAT_HERO__", CHAT_HERO)
            .replace("__CHAT_MODE__", CHAT_MODE)
            .replace("__SKILLS__", SKILLS_HTML)
            .replace("__HERO_TABS__", HERO_TABS))

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(CONFIG.get("port", 7790)))
    ap.add_argument("--host", default=CONFIG.get("host", "0.0.0.0"))
    args = ap.parse_args()
    s = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Dashboard at http://{args.host}:{args.port}", file=sys.stderr)
    try: s.serve_forever()
    except KeyboardInterrupt: s.server_close()

if __name__ == "__main__":
    raise SystemExit(main())
