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

# Loopback-bound ttyd terminals, reverse-proxied under /term/<id>/ behind this
# dashboard's Basic Auth. Nothing writable listens on 0.0.0.0 anymore.
TERM_PORTS = {"hermes": int(CONFIG.get("term_port", 7791))}

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
    """GREEN only when the pane is reachable AND the agent process is alive —
    a reachable ttyd whose REPL died renders the holding screen, and the
    status must say so instead of a false 'All's well'."""
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

def get_checks():
    """Run all checks in parallel."""
    checks = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(f) for f in (_check_chat, _check_mcp, _check_cron, _check_disk)]
        for f in as_completed(futs, timeout=10):
            try: checks.append(f.result())
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
        if not self._check_auth(): return
        p = urlparse(self.path).path
        if p.startswith("/term/"):
            return self._proxy_term()
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
                self._json({"checks":checks,"events":[],"notes":notes})
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
   display:flex;flex-direction:column;overflow:hidden;-webkit-font-smoothing:antialiased}
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

 <main id="chat"><iframe src="/term/hermes/?fontSize=16" title="Chat with Sasha"></iframe></main>

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
function trk(e){try{fetch('/api/t?e='+encodeURIComponent(e))}catch(_){}}

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

async function ap(p,m){const r=await fetch(p,{method:m||'GET'});return r.json()}

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

refreshAll();setInterval(refreshAll,30000);
</script>
</body>
</html>"""

HTML = (HTML.replace("__NAME__", NAME).replace("__PLACE__", PLACE)
            .replace("__CHIPS__", CHIPS_HTML)
            .replace("__COACH__", json.dumps(COACH_MAP))
            .replace("__NICE__", json.dumps(NICE_MAP)))

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
