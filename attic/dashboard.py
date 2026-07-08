#!/usr/bin/env python3
"""AI-OS Dashboard — fast, parallel checks, server-side rendered skills."""
import json, os, subprocess, sys, sqlite3, time, urllib.request, socket, select, threading
from datetime import datetime
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import base64

CC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CC)
HOME = os.path.expanduser("~")
AGENTS_DIR = os.path.join(HOME, "notes", "06 Logs", "Agents")

# Loopback-bound ttyd terminals, reverse-proxied under /term/<id>/ behind this
# dashboard's Basic Auth. Nothing writable listens on 0.0.0.0 anymore.
TERM_PORTS = {"bash": 8081, "hermes": 8082}

# ---- INT-4 usage telemetry: aggregate EVENT COUNTS only. One JSONL line per
# UI event ({ts, e, ip}) — never search queries, terminal content, or any
# transcript. /api/usage serves per-day aggregates + a 30-min-gap session
# estimate for the INT-4 reach report.
USAGE_PATH = os.path.join(HOME, ".aios-usage.jsonl")
_usage_lock = threading.Lock()

def track(event, ip=""):
    e = (event or "")[:48]
    if not e or not e.replace(":", "").replace("-", "").replace("_", "").replace(".", "").isalnum():
        return
    try:
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

def _check_daemons():
    wd = _pgrep("watchdog.py"); ar = _pgrep("agent_runner.py")
    return ("Daemons","GREEN" if wd and ar else "RED", f"watchdog {'✓'if wd else'✗'} runner {'✓'if ar else'✗'}")

def _check_ollama():
    tags = _api_json("http://192.168.86.21:11434/api/tags", 2)
    if tags:
        loaded = _api_json("http://192.168.86.21:11434/api/ps", 2)
        l = loaded["models"][0]["name"] if loaded and loaded.get("models") else "cold"
        return (".21","GREEN",f"up · {len(tags.get('models',[]))} models · {l}")
    return (".21","RED","unreachable")

def _check_mcp():
    global _cache
    now = time.time()
    if _cache["mcp"] and now - _cache["mcp_ts"] < 60:
        return _cache["mcp"]
    try:
        y = Path(os.path.join(HOME,".hermes","config.yaml")).read_text()
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
        r = ("MCP","GREEN" if enabled==servers and servers>=7 else "YELLOW",f"{enabled}/{servers} servers")
        _cache["mcp"] = r; _cache["mcp_ts"] = now
        return r
    except:
        return ("MCP","YELLOW","config?")

def _check_cron():
    global _cache
    now = time.time()
    if _cache["cron"] and now - _cache["cron_ts"] < 60:
        return _cache["cron"]
    try:
        r = subprocess.run(["/home/cvande/.local/bin/hermes","cron","list"], capture_output=True, text=True, timeout=5)
        n = sum(1 for l in r.stdout.split("\n") if "[active]" in l and l[:1] in " \t")
        rv = ("Cron","GREEN" if n>=3 else "YELLOW",f"{n} active")
        _cache["cron"] = rv; _cache["cron_ts"] = now
        return rv
    except:
        return ("Cron","YELLOW","?")

def _check_events_db():
    try:
        db = os.path.join(CC,"_lib","event_bus_data","events.db")
        if os.path.exists(db):
            conn = sqlite3.connect(db); row = conn.execute("SELECT COUNT(*),COALESCE(SUM(processed),0) FROM events").fetchone(); conn.close()
            t,p = row; pct = int(p/t*100) if t else 0
            return ("Events","GREEN" if pct>=50 else "YELLOW",f"{t} · {pct}%")
        return ("Events","YELLOW","no events")
    except:
        return ("Events","YELLOW","?")

def _check_cost():
    try:
        req = urllib.request.Request("https://api.deepseek.com/user/balance",
            headers={"Authorization":"Bearer "+_get_ds_key(),"Accept":"application/json"})
        with urllib.request.urlopen(req, timeout=4) as r:
            infos = json.loads(r.read()).get("balance_infos",[])
            bal = float(infos[0]["total_balance"]) if infos else 0
        return ("Cost","GREEN" if bal>5 else "YELLOW",f"${bal:.2f}")
    except:
        return ("Cost","YELLOW","?")

def _check_knowledge():
    ki = os.path.join(CC,"_lib","knowledge_index_data","index.npz")
    mi = os.path.join(CC,"_lib","knowledge_index_data","meta.jsonl")
    if os.path.exists(ki):
        ah = (time.time()-os.path.getmtime(ki))/3600
        n = sum(1 for _ in open(mi)) if os.path.exists(mi) else 0
        return ("Knowledge","GREEN" if ah<28 else "YELLOW",f"{n} passages · {ah:.0f}h")
    return ("Knowledge","YELLOW","not built")

def _check_agents():
    if os.path.exists(AGENTS_DIR):
        t = datetime.now().strftime("%Y-%m-%d"); td = sum(1 for f in os.listdir(AGENTS_DIR) if f.startswith(t)); tt = len(os.listdir(AGENTS_DIR))
        return ("Agents","GREEN",f"{td} today · {tt} total")
    return ("Agents","GREEN","no output")

def get_checks():
    """Run all checks in parallel with ThreadPoolExecutor."""
    checks = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {
            ex.submit(_check_daemons): 1,
            ex.submit(_check_ollama): 1,
            ex.submit(_check_mcp): 1,
            ex.submit(_check_cron): 1,
            ex.submit(_check_events_db): 1,
            ex.submit(_check_cost): 1,
            ex.submit(_check_knowledge): 1,
            ex.submit(_check_agents): 1,
        }
        for f in as_completed(futs, timeout=8):
            try: checks.append(f.result())
            except: pass
    return checks

def _get_ds_key():
    k = os.environ.get("DEEPSEEK_API_KEY")
    if k: return k
    try:
        for l in open(os.path.expanduser("~/.hermes/.env")):
            if l.startswith("DEEPSEEK_API_KEY="): return l.split("=",1)[1].strip().strip("\"'")
    except: pass
    return ""

def _run_ingest_plan():
    """Run ingest.py --plan and return structured results."""
    ingest_py = os.path.join(CC, "wiki", "ingest", "ingest.py")
    try:
        r = subprocess.run([sys.executable, ingest_py, "--plan"],
                           capture_output=True, text=True, timeout=30,
                           cwd=os.path.join(CC, "wiki", "ingest"))
        out = (r.stdout or "").strip() + (("\n" + r.stderr.strip()) if r.stderr.strip() else "")
        # Parse output for pending sources
        sources = []
        for line in (r.stdout or "").split("\n"):
            line = line.strip()
            if line and not line.startswith("[") and "inbox" not in line.lower() and "clear" not in line.lower() and "no unprocessed" not in line.lower():
                sources.append(line)
        # Count from final summary line
        count = 0
        for line in reversed((r.stdout or "").split("\n")):
            import re
            m = re.search(r"(\d+)\s+source", line.lower())
            if m:
                count = int(m.group(1))
                break
        if not count:
            count = len(sources)
        return {"exit": r.returncode, "count": count, "sources": sources[:30],
                "output": out[:2000], "error": r.stderr.strip()[:500] if r.returncode else ""}
    except subprocess.TimeoutExpired:
        return {"exit": 1, "error": "timeout"}
    except Exception as e:
        return {"exit": 1, "error": str(e)}

def _run_board_consult(question, only=""):
    """Run board/ask.py --run and return structured results."""
    ask_py = os.path.join(CC, "board", "ask.py")
    cmd = [sys.executable, ask_py, "--run"]
    if only:
        cmd.extend(["--only", only])
    cmd.append(question)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                           cwd=os.path.join(CC, "board"))
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()[:500]
        return {"exit": r.returncode, "output": out[:6000], "error": err, "question": question, "only": only}
    except subprocess.TimeoutExpired:
        return {"exit": 1, "error": "Board consult timed out (120s)"}
    except Exception as e:
        return {"exit": 1, "error": str(e)}

# ---- HTTP Server ----

class Handler(BaseHTTPRequestHandler):
    def _json(self, d, s=200):
        self.send_response(s); self.send_header("Content-Type","application/json"); self.send_header("Access-Control-Allow-Origin","*"); self.end_headers()
        self.wfile.write(json.dumps(d, ensure_ascii=False).encode())
    def _html(self, c, s=200):
        self.send_response(s); self.send_header("Content-Type","text/html; charset=utf-8"); self.end_headers()
        self.wfile.write(c.encode())

    def _check_auth(self):
        """HTTP Basic Auth. Env vars DASH_USER/DASH_PASS override file-based auth."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="AI-OS"')
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Authorization required")
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            user, pwd = decoded.split(":", 1)
            # Try env vars first, then file
            expected_user = os.environ.get("DASH_USER")
            expected_pwd = os.environ.get("DASH_PASS")
            if expected_user and expected_pwd:
                if user == expected_user and pwd == expected_pwd:
                    return True
            else:
                auth_file = os.environ.get("DASH_AUTH_FILE", os.path.join(HOME, ".key", "dash-auth"))
                if os.path.isfile(auth_file):
                    stored = open(auth_file).read().strip()
                    expected_user, expected_pwd = stored.split(":", 1)
                    if user == expected_user and pwd == expected_pwd:
                        return True
        except: pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="AI-OS"')
        self.end_headers()
        self.wfile.write(b"Invalid credentials")
        return False

    def _serve_static(self, path):
        """Serve static files from doc directories under the same auth domain."""
        # Map URL paths to filesystem dirs (mirrors nginx volume mounts)
        root_map = {
            "/reports":        os.path.join(HOME, "Github", "CC", "reports"),
            "/weather":        os.path.join(HOME, "Github", "CC", "ranch-weather", "docs"),
            "/qrz":            os.path.join(HOME, "Github", "CC", "qrz-logbook", "docs"),
            "/frigate":        os.path.join(HOME, "Github", "CC", "frigate", "docs"),
            "/learn":          os.path.join(HOME, "Github", "CC", "learn"),
        }
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

    def _is_auth_ok(self):
        """Check auth without sending 401 on failure — just return bool."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            user, pwd = decoded.split(":", 1)
            expected_user = os.environ.get("DASH_USER")
            expected_pwd = os.environ.get("DASH_PASS")
            if expected_user and expected_pwd:
                return user == expected_user and pwd == expected_pwd
            auth_file = os.environ.get("DASH_AUTH_FILE", os.path.join(HOME, ".key", "dash-auth"))
            if os.path.isfile(auth_file):
                stored = open(auth_file).read().strip()
                e_user, e_pwd = stored.split(":", 1)
                return user == e_user and pwd == e_pwd
        except: pass
        return False

    def _proxy_term(self):
        """Transparently reverse-proxy /term/<id>/... to a loopback ttyd, under
        this dashboard's auth. One raw-socket tunnel serves both the HTTP asset
        fetches and the WebSocket upgrade — protocol-agnostic once bytes flow."""
        parts = urlparse(self.path).path.split("/", 3)
        tid = parts[2] if len(parts) > 2 else ""
        port = TERM_PORTS.get(tid)
        if port is None and tid.startswith("t") and tid[1:].isdigit():
            pt = int(tid[1:])
            if 8086 <= pt <= 8110:  # dynamically-added terminals
                port = pt
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
        blocking mode. Blocking sends apply natural backpressure: a full kernel
        send buffer makes sendall wait instead of raising EAGAIN, so a bursty
        terminal (tmux full-screen redraws, WebGL output) can't tear the tunnel
        down. The old select-loop ran the sockets non-blocking and treated a
        send-side EAGAIN (a BlockingIOError, i.e. OSError) as fatal — which is
        exactly what dropped live terminals to 'green then red' under load."""
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
                # Unblock the peer thread's blocking recv() and signal EOF.
                for s in (src, dst):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        t = threading.Thread(target=one_way, args=(b, a), daemon=True)
        t.start()
        one_way(a, b)
        t.join(timeout=5)

    # Static paths that don't require auth
    PUBLIC_PREFIXES = ("/weather", "/qrz", "/reports", "/learn", "/frigate")

    def do_GET(self):
        p = urlparse(self.path).path
        # Skip auth for public static paths
        if not p.startswith(self.PUBLIC_PREFIXES):
            if not self._check_auth(): return
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
        elif p == "/api/events":
            try:
                b = __import__("_lib.event_bus", fromlist=["EventBus"]).EventBus()
                self._json({"events":[{"id":e["id"],"ts":e["ts"],"source":e["source"],"type":e["type"]} for e in list(b.subscribe(since_id=0,limit=100))[-30:]]})
            except Exception as e: self._json({"error":str(e)})
        elif p == "/api/agents":
            try:
                n = []
                for f in sorted(os.listdir(AGENTS_DIR), reverse=True)[:15] if os.path.exists(AGENTS_DIR) else []:
                    if not f.endswith(".md"): continue
                    fp = os.path.join(AGENTS_DIR, f); c = open(fp).read()[:200]
                    n.append({"file":f,"preview":c[:150]})
                self._json({"notes":n})
            except Exception as e: self._json({"error":str(e)})
        elif p == "/api/exec":
            cmd = urlparse(self.path).query
            if cmd:
                try:
                    r = subprocess.Popen(["bash","-c",cmd+" &>/dev/null &"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self._json({"exit":0, "pid":r.pid})
                except Exception as e: self._json({"error":str(e)})
            else: self._json({"error":"no cmd"})
        elif p == "/api/search":
            q = urlparse(self.path).query
            if q.startswith("q="):
                q = unquote(q[2:])
                r = subprocess.run([sys.executable,os.path.join(CC,"_lib","knowledge_search.py"),q], capture_output=True, text=True, timeout=30)
                try: self._json(json.loads(r.stdout))
                except: self._json({"error":(r.stderr or r.stdout or "?").strip()[:200]})
            else: self._json({"error":"no query"})
        elif p == "/api/all":
            try:
                checks = [{"label":l,"status":s,"detail":d} for l,s,d in get_checks()]
                # Events
                try:
                    b = __import__("_lib.event_bus", fromlist=["EventBus"]).EventBus()
                    evs = [{"source":e["source"],"type":e["type"],"ts":e["ts"]} for e in list(b.subscribe(since_id=0,limit=100))[-30:]]
                except: evs = []
                # Agents
                try:
                    notes = []
                    for f in sorted(os.listdir(AGENTS_DIR), reverse=True)[:15] if os.path.exists(AGENTS_DIR) else []:
                        if not f.endswith(".md"): continue
                        fp = os.path.join(AGENTS_DIR, f); c = open(fp).read()[:200]
                        notes.append({"file":f,"preview":c[:150]})
                except: notes = []
                self._json({"checks":checks,"events":evs,"notes":notes})
            except Exception as e: self._json({"error":str(e)})
        elif p == "/api/read":
            q = urlparse(self.path).query
            if q.startswith("path="):
                fp = unquote(q[5:])
                # Handle relative paths by trying known base directories
                if not fp.startswith("/"):
                    bases = [os.path.expanduser("~/notes"), os.path.expanduser("~/.claude/projects"), CC]
                    for b in bases:
                        candidate = os.path.join(b, fp)
                        if os.path.isfile(candidate):
                            fp = candidate
                            break
                allowed = [os.path.expanduser("~/.claude/projects"), os.path.expanduser("~/notes"), CC]
                ok = any(fp.startswith(a) for a in allowed)
                if ok and os.path.isfile(fp):
                    try:
                        c = open(fp, encoding="utf-8", errors="replace").read()
                        self._json({"path":fp,"content":c[:3000]})
                    except Exception as e:
                        self._json({"error":str(e)})
                else: self._json({"error":"path not allowed or not found"})
            else: self._json({"error":"no path"})
        else:
            # Static files — no extra auth needed; already authed to reach dashboard
            self._serve_static(p)

    def do_POST(self):
        if not self._check_auth(): return
        p = urlparse(self.path).path
        if p.startswith("/term/"):
            return self._proxy_term()
        if p == "/api/run/inbox-triage":
            r = subprocess.run([sys.executable,os.path.join(CC,"_lib","inbox_triage.py")], capture_output=True, text=True, timeout=120)
            self._json({"output":r.stdout.strip()[:500]})
        elif p == "/api/run/knowledge-gardener":
            r = subprocess.run([sys.executable,os.path.join(CC,"_lib","knowledge_gardener.py")], capture_output=True, text=True, timeout=120)
            self._json({"output":r.stdout.strip()[:500]})
        elif p == "/api/run/ingest":
            try:
                r = _run_ingest_plan()
                self._json(r)
            except Exception as e:
                self._json({"exit": 1, "error": str(e)})
        elif p == "/api/run/board":
            try:
                clen = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(clen).decode() if clen else "{}"
                data = json.loads(body) if body else {}
                r = _run_board_consult(data.get("question", "Strategic check-in?"),
                                       only=data.get("only", ""))
                self._json(r)
            except Exception as e:
                self._json({"exit": 1, "error": str(e)})
        elif p == "/api/run/event-bus-stats":
            try:
                db = os.path.join(CC,"_lib","event_bus_data","events.db")
                if os.path.exists(db):
                    conn = sqlite3.connect(db)
                    row = conn.execute("SELECT COUNT(*),COALESCE(SUM(processed),0),MIN(id),MAX(id),MIN(ts),MAX(ts) FROM events").fetchone()
                    conn.close()
                    t, pct_sum, mn, mx, ts_min, ts_max = row
                    pct = int(pct_sum/t*100) if t else 0
                    self._json({"output":"Events: %d total · %d%% processed · %s to %s" % (t, pct, (ts_min or "?")[11:19], (ts_max or "?")[11:19])})
                else:
                    self._json({"output":"Events: no database"})
            except Exception as e:
                self._json({"output":"Events: error — %s" % str(e)})
        elif p.startswith("/api/ack/"):
            try: __import__("_lib.event_bus", fromlist=["EventBus"]).EventBus().ack(int(p.split("/")[-1])); self._json({"ok":True})
            except: self._json({"error":"bad id"},400)
        else: self._json({"error":"not found"},404)

    def log_message(self, f, *a):
        if "/api/" in str(a): print("[%s] %s" % (self.log_date_time_string(), f % a), file=sys.stderr)


# ---- Skills (auto-discovered from ~/.hermes/skills/) ----

# Default emoji mapping — extend or override by creating ~/.hermes/skills/.emoji.json
DEFAULT_SKILL_ICONS = {
    "backup":"💾","restore":"♻️","triage":"📥","status":"❤️",
    "recall":"🧠","wiki":"📚","ingest":"📝","teach":"📖",
    "secret":"🔑","improve":"✨","board":"📋","capture":"📸",
    "firealert":"🚨","product":"📦","weather":"🌤️","energy":"⚡",
    "health":"❤️","search":"🔍","garden":"🌱","cron":"⏰",
}

def _discover_skills():
    """Scan ~/.hermes/skills/ for available skills and auto-discover."""
    skills_dir = os.path.join(HOME, ".hermes", "skills")
    icons = DEFAULT_SKILL_ICONS.copy()
    icon_file = os.path.join(skills_dir, ".emoji.json")
    if os.path.isfile(icon_file):
        try:
            icons.update(json.loads(open(icon_file).read()))
        except: pass
    discovered = []
    if os.path.isdir(skills_dir):
        for name in sorted(os.listdir(skills_dir)):
            d = os.path.join(skills_dir, name)
            if os.path.isdir(d) and not name.startswith("."):
                ico = icons.get(name, "🔧")
                discovered.append((name, ico))
    return discovered

SKILLS = _discover_skills()

# Curated favorites — skills you use often (others go under collapsible "All Skills")
# Order matters: top items appear first in the sidebar
FAVORITE_SKILLS = [
    "triage", "recall", "wiki", "ingest", "board",
    "backup", "restore", "signal-scan",
]

SKILLS_HTML = """<h2>Skills</h2><div id="sk">"""
if SKILLS:
    fav_set = set(FAVORITE_SKILLS)
    favs = [(n,i) for n,i in SKILLS if n in fav_set]
    others = [(n,i) for n,i in SKILLS if n not in fav_set]

    # Favorites
    SKILLS_HTML += '<div class="sh">Favorites</div>'
    if favs:
        for name, ico in favs:
            SKILLS_HTML += f'<div class="sk" onclick="runSkill(\'{name}\')"><span class="sk-ico">{ico}</span><span class="sk-n">{name}</span></div>'
    else:
        SKILLS_HTML += '<div class="sk" style="color:var(--faint);cursor:default;font-size:11px">None selected</div>'

    # Collapsible "All Skills" section
    SKILLS_HTML += f'<div class="sh sh-c" onclick="toggleAllSkills()" id="all-sk-toggle">All Skills ({len(others)}) <span id="all-sk-arrow">▸</span></div><div id="all-sk-list" style="display:none">'
    for name, ico in others:
        SKILLS_HTML += f'<div class="sk" onclick="runSkill(\'{name}\')"><span class="sk-ico">{ico}</span><span class="sk-n">{name}</span></div>'
    SKILLS_HTML += "</div>"
else:
    SKILLS_HTML += '<div class="sh">No skills found</div><div class="sk" style="color:var(--faint);cursor:default;font-size:11px">Run <code>hermes setup</code> first</div>'
SKILLS_HTML += "</div>"

# ---- Services (loaded from config file) ----
SERVICES_FILE = os.path.join(CC, "_lib", "services.json")
SERVICES_HTML = """<h2>Services</h2><div class="sc-grid" id="svc">"""
if os.path.isfile(SERVICES_FILE):
    try:
        for svc in json.loads(open(SERVICES_FILE).read()):
            href = svc.get("href","")
            label = svc.get("label","")
            desc = svc.get("desc","")
            ico = svc.get("ico","🔗")
            ext = svc.get("ext","↗")
            SERVICES_HTML += f'<a class="sc-card" href="{href}" target="_blank"><span class="sc-ico">{ico}</span><div class="sc-body"><div class="sc-t">{label}</div><div class="sc-d">{desc}</div></div><span class="sc-ext">{ext}</span></a>\n'
    except: pass
else:
    SERVICES_HTML += '<div class="sh" style="color:var(--faint);font-size:11px;padding:8px 0">No services configured. Create <code>_lib/services.json</code></div>'
SERVICES_HTML += "</div>"

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI-OS</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root{--bg:#08090a;--panel:#0f1011;--surf:#191a1b;--surf-hov:#23252a;--ink:#f7f8f8;--dim:#d0d6e0;--mute:#8a8f98;--faint:#62666d;--accent:#5e6ad2;--accent-on:#7170ff;--accent-hov:#828fff;--green:#27a644;--green-bg:rgba(39,166,68,0.12);--warn:#f5a623;--warn-bg:rgba(245,166,35,0.12);--bad:#f85149;--bad-bg:rgba(248,81,73,0.12);--border:rgba(255,255,255,0.08);--border-s:rgba(255,255,255,0.05);--rad:6px;--rad-c:8px;--rad-l:12px;--font:'Inter',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;--mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace}
  *{margin:0;padding:0;box-sizing:border-box}
 body{font-family:var(--font);background:var(--bg);color:var(--dim);display:flex;height:100vh;overflow:hidden;font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased;font-feature-settings:"cv01","ss03"}
 ::-webkit-scrollbar{width:5px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.08);border-radius:4px}::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,0.12)}

 /* ── Sidebar ──────────────────────────────────── */
 #side{width:230px;min-width:230px;background:var(--panel);border-right:1px solid var(--border-s);display:flex;flex-direction:column;overflow-y:auto}
 #side h2{font-size:11px;font-weight:600;padding:16px 14px 6px;color:var(--faint);text-transform:uppercase;letter-spacing:.06em}
 .sh{font-size:10px;font-weight:600;color:var(--faint);text-transform:uppercase;letter-spacing:.08em;padding:14px 14px 5px;margin-top:6px;border-top:1px solid var(--border-s);padding-top:12px}
 .sh:first-of-type{border-top:none;margin-top:0}
 .sk{display:flex;align-items:center;padding:5px 10px;font-size:13px;cursor:pointer;color:var(--mute);border-radius:var(--rad);margin:1px 8px;transition:all .12s}
 .sk:hover{background:rgba(255,255,255,0.04);color:var(--ink)}
 .sk:active{background:rgba(255,255,255,0.06);transform:scale(0.98)}
 .sk-ico{width:22px;text-align:center;font-size:13px;margin-right:6px;flex:none}
 .sk-n{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .sk-d{font-size:10px;color:var(--faint);margin-left:6px;white-space:nowrap;font-weight:500}

 /* ── Collapsible skills section ─────────────── */
 .sh-c{cursor:pointer;user-select:none;transition:color .12s}
 .sh-c:hover{color:var(--dim)}
 #all-sk-arrow{float:right;transition:transform .2s;font-size:12px}
 #all-sk-arrow.open{transform:rotate(90deg)}
 #all-sk-list{overflow:hidden;transition:max-height .25s ease}


 /* ── Main layout ──────────────────────────────── */
 #main{flex:1;display:flex;flex-direction:column;overflow:hidden}

 /* ── Top bar ───────────────────────────────────── */
 #top{display:flex;align-items:center;gap:8px;padding:6px 14px;background:var(--panel);border-bottom:1px solid var(--border-s);min-height:40px}
 #top h1{font-size:15px;font-weight:600;margin-right:auto;color:var(--ink);letter-spacing:-.01em}
 .bg{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;padding:3px 10px;border-radius:999px;letter-spacing:.02em}
 .bg-green{background:var(--green-bg);color:var(--green)}
 .bg-yellow{background:var(--warn-bg);color:var(--warn)}
 .bg-red{background:var(--bad-bg);color:var(--bad)}
 #bt{font-size:12px!important;color:var(--mute)!important;font-weight:400!important}
 .btn{background:rgba(255,255,255,0.04);border:1px solid var(--border);border-radius:var(--rad);color:var(--dim);padding:5px 12px;font-size:12px;cursor:pointer;transition:all .12s;font-weight:500;line-height:1.3}
 .btn:hover{background:rgba(255,255,255,0.08);border-color:rgba(255,255,255,0.12);color:var(--ink)}
 .btn:active{transform:scale(0.97)}
 .btn-pri{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
 .btn-pri:hover{background:var(--accent-hov)!important;border-color:var(--accent-hov)!important}

 /* ── Terminal area ─────────────────────────────── */
 #term{flex:1;display:flex;flex-direction:column;min-height:0}
 #ttabs{display:flex;background:var(--panel);border-bottom:1px solid var(--border-s);min-height:28px}
 .tt{padding:4px 14px;font-size:12px;cursor:pointer;color:var(--mute);border-bottom:2px solid transparent;display:flex;align-items:center;gap:4px;transition:all .1s;font-weight:500}
 .tt:hover{color:var(--dim)}
 .tt-a{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}
 .tx{font-size:11px;color:var(--faint);margin-left:4px;cursor:pointer;padding:0 3px;border-radius:3px;line-height:1}
 .tx:hover{color:var(--bad);background:var(--bad-bg)}
 .ta{font-size:16px;color:var(--faint);padding:3px 10px;cursor:pointer;font-weight:600;line-height:1}
 .ta:hover{color:var(--dim)}
 #tframes{flex:1;position:relative}
 .tf{width:100%;height:100%;border:none;position:absolute;top:0;left:0}
 .tf-h{display:none}

 /* ── Run-output bar ────────────────────────────── */
 #ro{display:none;align-items:center;gap:10px;padding:6px 14px;background:var(--surf);border-bottom:1px solid var(--border-s);font-size:13px;min-height:32px}
 #ro-l{font-weight:600;color:var(--accent-on);white-space:nowrap;font-size:12px}
 #ro-t{color:var(--dim);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 #ro-x{color:var(--faint);cursor:pointer;font-size:18px;padding:0 6px;border-radius:4px;line-height:1}
 #ro-x:hover{color:var(--bad);background:var(--bad-bg)}
 #ro-results{background:var(--panel);border-bottom:1px solid var(--border-s);padding:0 14px 10px;max-height:180px;overflow-y:auto}
 .rr-hide{display:none}
 .rr-card{display:flex;gap:10px;align-items:flex-start;padding:8px 10px;margin-bottom:4px;background:var(--bg);border:1px solid var(--border);border-radius:var(--rad-c);transition:border-color .12s}
 .rr-card:hover{border-color:rgba(255,255,255,0.12)}
 .rr-badge{font-size:9px;font-weight:700;padding:2px 8px;border-radius:999px;white-space:nowrap;flex:none;margin-top:2px;text-transform:uppercase;letter-spacing:.06em}
 .rr-urgent{background:var(--bad-bg);color:var(--bad)}
 .rr-fyi{background:var(--warn-bg);color:var(--warn)}
 .rr-noise{background:rgba(255,255,255,0.04);color:var(--faint)}
 .rr-body{flex:1;min-width:0}
 .rr-subj{font-weight:500;font-size:13px;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .rr-subj .rr-src{font-weight:400;font-size:10px;color:var(--faint);margin-left:4px}
 .rr-from{font-size:11px;color:var(--mute);margin-top:1px}
 .rr-snip{font-size:11px;color:var(--faint);margin-top:2px;line-height:1.4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

 /* ── Bottom panel tabs ─────────────────────────── */
 #bot{border-top:1px solid var(--border-s);max-height:38%;display:flex;flex-direction:column}
 #btabs{display:flex;background:var(--panel);border-bottom:1px solid var(--border-s)}
 .bt,.bt-r{padding:6px 16px;font-size:12px;cursor:pointer;color:var(--mute);border-bottom:2px solid transparent;transition:all .1s;font-weight:500}
 .bt:hover,.bt-r:hover{color:var(--dim)}
 .bt-a{color:var(--ink)!important;border-bottom-color:var(--accent)!important;font-weight:600!important}
 .bt-r{margin-left:auto}

 /* ── Bottom panel content ──────────────────────── */
 #bb{flex:1;overflow-y:auto;padding:8px 12px;font-size:14px}

 /* ── Status cards ──────────────────────────────── */
 .sg{display:grid;grid-template-columns:repeat(auto-fill,minmax(185px,1fr));gap:8px}
 .sc{background:var(--surf);border:1px solid var(--border);border-radius:var(--rad-c);padding:10px 12px;transition:all .12s}
 .sc:hover{border-color:rgba(255,255,255,0.12);background:var(--surf-hov)}
 .sch{display:flex;justify-content:space-between;align-items:center}
 .scl{font-weight:500;font-size:13px;color:var(--ink)}
 .b{padding:2px 10px;border-radius:999px;font-size:10px;font-weight:600;letter-spacing:.03em}
 .b-green{background:var(--green-bg);color:var(--green)}
 .b-yellow{background:var(--warn-bg);color:var(--warn)}
 .b-red{background:var(--bad-bg);color:var(--bad)}
 .scd{font-size:11px;color:var(--mute);margin-top:3px;line-height:1.4}

 /* ── Events table ──────────────────────────────── */
 .et{width:100%;font-size:12px;border-collapse:collapse}
 .et td{padding:5px 8px;border-bottom:1px solid var(--border-s)}
 .ets{color:var(--accent-on);font-weight:500;font-size:11px}
 .ett{color:#d2a8ff;font-size:11px}
 .etd{color:var(--faint);font-size:11px;font-family:var(--mono)}

 /* ── Agent notes ───────────────────────────────── */
 .an{background:var(--surf);border:1px solid var(--border);border-radius:var(--rad-c);padding:8px 12px;margin-bottom:5px;font-size:12px}
 .ant{font-weight:500;color:var(--ink)}
 .anp{color:var(--mute);font-size:11px;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;line-height:1.4}

 /* ── Last run timestamp ────────────────────────── */
 .lr{font-size:10px;color:var(--faint);padding:3px 14px;text-align:right;border-top:1px solid var(--border-s);font-family:var(--mono)}

 /* ── Services grid ─────────────────────────────── */
 .sc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;padding:8px}
 .sc-card{display:flex;gap:10px;align-items:flex-start;text-decoration:none;color:var(--dim);background:var(--surf);border:1px solid var(--border);border-radius:var(--rad-l);padding:10px 12px;transition:all .12s;cursor:pointer}
 .sc-card:hover{border-color:var(--accent);background:var(--surf-hov);transform:translateY(-1px)}
 .sc-card:active{transform:translateY(0)}
 .sc-ico{font-size:16px;flex:none;width:26px;height:26px;display:grid;place-items:center;background:var(--bg);border:1px solid var(--border);border-radius:var(--rad)}
 .sc-body{min-width:0;flex:1}
 .sc-t{font-weight:600;font-size:13px;color:var(--ink)}
 .sc-p{font-size:10px;color:var(--faint);margin-left:4px;font-weight:400}
 .sc-d{color:var(--mute);font-size:11px;margin-top:3px;line-height:1.4}
 .sc-ext{color:var(--faint);font-size:11px;margin-left:auto;transition:color .12s}
 .sc-card:hover .sc-ext{color:var(--accent-on)}

 /* ── Search panel ──────────────────────────────── */
 #sr-panel{display:flex;gap:8px;padding:8px 0}
 #sr-q{flex:1;background:rgba(255,255,255,0.03);border:1px solid var(--border);border-radius:var(--rad);color:var(--ink);padding:9px 12px;font-size:14px;outline:none;font-family:var(--font);transition:all .12s}
 #sr-q:focus{border-color:var(--accent);background:rgba(255,255,255,0.05)}
 #sr-q::placeholder{color:var(--faint)}
 #sr-btn{background:var(--accent);border:1px solid var(--accent);border-radius:var(--rad);color:#fff;padding:9px 20px;font-size:12px;cursor:pointer;font-weight:600;transition:all .12s;line-height:1.3}
 #sr-btn:hover{background:var(--accent-hov);border-color:var(--accent-hov)}
 #sr-btn:active{transform:scale(0.97)}
 #sr-info{color:var(--mute);font-size:11px;padding:4px 2px 8px}
 .sr-r{background:var(--surf);border:1px solid var(--border);border-radius:var(--rad-c);padding:10px 12px;margin-bottom:6px;cursor:pointer;transition:all .12s}
 .sr-r:hover{border-color:var(--accent);background:var(--surf-hov)}
 .sr-h{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
 .sr-s{color:var(--accent-on);font-size:10px;font-weight:700;font-family:var(--mono)}
 .sr-k{color:#d2a8ff;font-size:9px;font-weight:600;text-transform:uppercase;background:rgba(255,255,255,0.04);padding:1px 7px;border-radius:4px;letter-spacing:.04em}
 .sr-p{color:var(--faint);font-size:9px;margin-left:auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:200px;font-family:var(--mono)}
 .sr-t{color:var(--dim);font-size:12px;margin-top:4px;line-height:1.5}
 .sr-e{color:var(--bad);font-size:12px;padding:12px 0}
 .sr-none{color:var(--faint);font-size:12px;padding:20px 0;text-align:center}

/* ── Spinner ──────────────────────────────────── */
@keyframes spin{to{transform:rotate(360deg)}}
.sp{display:inline-block;width:16px;height:16px;border:2px solid var(--border);border-top-color:var(--accent-on);border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:6px}</style>
</head>
<body>
<div id="side">""" + SKILLS_HTML + """</div>
<div id="main">
 <div id="top">
  <h1>AI-OS</h1>
  <span id="bn" class="bg bg-green">&#9679;</span>
  <span id="bt" style="font-size:11px;color:#8b949e">—</span>
  <div style="flex:1"></div>
  <button class="btn" onclick="run('inbox-triage')">Triage</button>
  <button class="btn" onclick="run('knowledge-gardener')">Gardener</button>
  <button class="btn btn-pri" onclick="refreshAll()">&#x21bb;</button>
 </div>
 <div id="term">
  <div id="ttabs"></div>
  <div id="tframes"></div>
 </div>
<div id="ro"><span id="ro-l"></span><span id="ro-t"></span><span id="ro-x" onclick="document.getElementById('ro').style.display='none';document.getElementById('ro-results').style.display='none'">×</span></div>
<div id="ro-results" class="rr-hide"></div>
<div id="bot">
  <div id="btabs"><div class="bt bt-a" data-t="status" onclick="sw('status')">Status</div><div class="bt" data-t="services" onclick="sw('services')">Services</div><div class="bt" data-t="search" onclick="sw('search')">Search</div><div class="bt" data-t="ingest" onclick="sw('ingest');loadIngest()">Ingest</div><div class="bt" data-t="board" onclick="sw('board')">Board</div><div class="bt-r" data-t="events" onclick="sw('events')">Events</div><div class="bt" data-t="agents" onclick="sw('agents')">Agents</div></div>
  <div id="bb"><div id="t-status"><div class="sg" id="checks"></div></div><div id="t-events" style="display:none"><table class="et" id="etab"></table></div><div id="t-agents" style="display:none"><div id="atab"></div></div><div id="t-services" style="display:none">""" + SERVICES_HTML + """</div><div id="t-search" style="display:none"><div id="sr-panel"><input id="sr-q" type="text" placeholder="Ask anything — search knowledge, memory, vault..." onkeydown="if(event.key==='Enter')doSearch()"><button id="sr-btn" onclick="doSearch()">Search</button></div><div id="sr-info"></div><div id="sr-results"></div></div><div id="t-ingest" style="display:none"><div id="ing-panel"><div id="ing-top"><button class="btn btn-pri" onclick="loadIngest()">&#x21bb; Refresh</button><span id="ing-count" style="color:var(--faint);font-size:12px;margin-left:10px"></span></div><div id="ing-results"></div></div></div><div id="t-board" style="display:none"><div id="bd-panel"><div id="bd-input"><input id="bd-q" type="text" placeholder="What decision do you want to put to the board?" onkeydown="if(event.key==='Enter')runBoard()" style="flex:1;background:rgba(255,255,255,0.03);border:1px solid var(--border);border-radius:var(--rad);color:var(--ink);padding:9px 12px;font-size:14px;outline:none;font-family:var(--font);transition:all .12s"><input id="bd-only" type="text" placeholder="Advisors (comma sep, default all)" style="width:180px;background:rgba(255,255,255,0.03);border:1px solid var(--border);border-radius:var(--rad);color:var(--ink);padding:9px 12px;font-size:13px;outline:none;font-family:var(--mono);transition:all .12s"><button id="bd-btn" class="btn btn-pri" onclick="runBoard();this.textContent='Running...';this.disabled=true">Consult</button></div><div id="bd-status" style="color:var(--mute);font-size:12px;padding:6px 2px"></div><div id="bd-results" style="max-height:65vh;overflow-y:auto"></div></div></div></div>
 </div>
 <div class="lr" id="lr"></div>
</div>
<script>
// INT-4 telemetry beacon — event names only, fire-and-forget
function trk(e){try{fetch('/api/t?e='+encodeURIComponent(e))}catch(_){}}
// Terminal tabs — ttyd wraps tmux sessions so page refresh = reconnect to same session
const TERMS=[{id:'bash',l:'bash',p:8081,ts:'dash-bash',spawned:true},{id:'hermes',l:'hermes',p:8082,ts:'dash-hermes',spawned:true}]; let tc=5;
function tmuxCmd(p,prog){
  var sess='dash-t'+p;
  if(prog==='hermes') return 'ttyd -i 127.0.0.1 --port '+p+' --writable -b /term/hermes tmux new-session -A -s dash-hermes hermes';
  return 'ttyd -i 127.0.0.1 --port '+p+' --writable -b /term/t'+p+' tmux new-session -A -s dash-t'+p;
}
function ensureTerm(t){
  if(t.spawned)return;
  fetch('/api/exec?'+encodeURIComponent(tmuxCmd(t.p,t.l==='hermes'?'hermes':'bash')));
  t.spawned=true;
}
function rt(){
  document.getElementById('ttabs').innerHTML=TERMS.map(t=>'<div class="tt'+(t.a?' tt-a':'')+'" onclick="st(\\''+t.id+'\\')">'+t.l+'<span class="tx" onclick="event.stopPropagation();ct(\\''+t.id+'\\')">&times;</span></div>').join('')+'<span class="ta" onclick="at()">+</span>';
  document.getElementById('tframes').innerHTML=TERMS.map(t=>'<iframe class="tf'+(t.a?'':' tf-h')+'" src="/term/'+t.id+'/"></iframe>').join('');
  TERMS.forEach(t=>ensureTerm(t));
}
function st(i){trk('tab:'+i);TERMS.forEach(t=>t.a=(t.id===i));rt()}
function ct(i){if(TERMS.length<2)return; const x=TERMS.findIndex(t=>t.id===i);fetch('/api/exec?'+encodeURIComponent('tmux kill-session -t '+TERMS[x].ts+' 2>/dev/null; pkill -f "ttyd.*:'+TERMS[x].p+'"'));TERMS.splice(x,1);TERMS[0].a=1;rt()}
function at(){trk('addterm');const p=8086+tc++;fetch('/api/exec?'+encodeURIComponent(tmuxCmd(p,'bash')));TERMS.forEach(t=>t.a=0);TERMS.push({id:'t'+p,l:'b'+p,p,ts:'dash-t'+p,spawned:true,a:1});rt()}
TERMS[0].a=1;rt();

// Bottom tabs
function sw(n){
  trk('btab:'+n);
  document.querySelectorAll('.bt,.bt-r').forEach(t=>t.classList.toggle('bt-a',t.dataset.t===n));
  ['status','events','agents','services','search','ingest','board'].forEach(x=>{
    const el=document.getElementById('t-'+x);
    if(el)el.style.display=x===n?'block':'none'
  })
}

async function ap(p,m,b){const o={method:m||'GET'};if(b){o.headers={'Content-Type':'application/json'};o.body=JSON.stringify(b)}const r=await fetch(p,o);return r.json()}

async function refreshAll(){
  const d=await ap('/api/all'); const bn=document.getElementById('bn'); const bt=document.getElementById('bt');
  if(d.error){bn.className='bg bg-red';bt.textContent='Err';return}
  const o=d.checks||[]; const red=o.some(c=>c.status==='RED'); const yel=o.some(c=>c.status==='YELLOW');
  const os=red?'RED':yel?'YELLOW':'GREEN';
  bn.className='bg bg-'+os.toLowerCase(); bt.textContent=os==='GREEN'?'OK':os==='YELLOW'?yel+' warn':red+' err';
  document.getElementById('checks').innerHTML=o.map(c=>'<div class="sc"><div class="sch"><span class="scl">'+c.label+'</span><span class="b b-'+c.status.toLowerCase()+'">'+c.status+'</span></div><div class="scd">'+(c.detail||'')+'</div></div>').join('');
  const es=(d.events||[]).slice(-25);
  document.getElementById('etab').innerHTML=es.map(e=>'<tr><td class="ets">'+e.source+'</td><td class="ett">'+e.type+'</td><td class="etd">'+(e.ts||'').slice(11,19)+'</td></tr>').join('');
  document.getElementById('atab').innerHTML=(d.notes||[]).slice(0,12).map(n=>'<div class="an"><div class="ant">'+n.file+'</div><div class="anp">'+(n.preview||'').slice(0,120)+'</div></div>').join('');
  document.getElementById('lr').textContent=new Date().toLocaleTimeString();
}

// Search
async function doSearch(){
  const q=document.getElementById('sr-q').value.trim(); if(!q)return;
  trk('search'); // that a search happened — never the query
  const info=document.getElementById('sr-info'),res=document.getElementById('sr-results');
  info.textContent='Searching...'; res.innerHTML='';
  const d=await ap('/api/search?q='+encodeURIComponent(q));
  if(d.error){info.textContent='';res.innerHTML='<div class="sr-e">Error: '+d.error+'</div>';return}
  if(!d.results||d.results.length===0){info.textContent='';res.innerHTML='<div class="sr-none">No results found</div>';return}
  info.textContent=d.count+' result'+(d.count>1?'s':'')+' for "'+d.query+'"';
  res.innerHTML=d.results.map((r,i)=>'<div class="sr-r" onclick="expandResult('+i+')" data-idx="'+i+'"><div class="sr-h"><span class="sr-s">'+r.score.toFixed(2)+'</span><span class="sr-k">'+r.kind+'</span><span class="sr-p">'+r.path.split('/').pop()+'</span></div><div class="sr-t" id="sr-t-'+i+'">'+esc(r.text)+'</div><div class="sr-c" id="sr-c-'+i+'" style="display:none"></div><div class="sr-f" id="sr-f-'+i+'" style="display:none;font-size:11px;color:#484f58;margin-top:3px">load full note...</div></div>').join('');
  window._sr = d.results;
}
async function expandResult(i){
  const c=document.getElementById('sr-c-'+i),f=document.getElementById('sr-f-'+i),t=document.getElementById('sr-t-'+i);
  if(c.style.display==='block'){c.style.display='none';f.style.display='none';t.style.display='block';return}
  if(c.textContent){c.style.display='block';f.style.display='none';t.style.display='none';return}
  f.style.display='block';f.textContent='loading...';
  const r=window._sr[i],d=await ap('/api/read?path='+encodeURIComponent(r.path));
  f.style.display='none';
  if(d.error){c.innerHTML='<div class="sr-e">Error loading: '+d.error+'</div>';c.style.display='block';t.style.display='none';return}
  c.innerHTML='<pre style="font-size:12px;color:#8b949e;line-height:1.5;margin-top:4px;overflow-x:auto;white-space:pre-wrap">'+esc(d.content)+'</pre>';
  c.style.display='block';t.style.display='none';
}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}

function escMd(s){
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
         .replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/`(.+?)`/g,'<code>$1</code>')
         .replace(/^### (.+)$/gm,'<h4 style="color:var(--accent-on);margin:12px 0 4px;font-size:13px">$1</h4>')
         .replace(/^## (.+)$/gm,'<h3 style="color:var(--ink);margin:14px 0 4px;font-size:14px">$1</h3>')
         .replace(/\\n{2,}/g,'</p><p style="margin:6px 0;line-height:1.5">')
         .replace(/^- (.+)$/gm,'<span style="display:block;padding:1px 0 1px 14px;position:relative">• $1</span>')
         .replace(/\\n/g,'<br>')
}

async function loadIngest(){
  const info=document.getElementById('ing-count'),res=document.getElementById('ing-results');
  info.textContent='Scanning inbox...'; res.innerHTML='';
  const d=await ap('/api/run/ingest','POST');
  if(d.error){info.textContent='Error';res.innerHTML='<div class="sr-e">'+esc(d.error)+'</div>';return}
  info.textContent=d.exit===0 ? d.count+' source'+(d.count!==1?'s':'')+' pending' : 'exit code '+d.exit;
  if(d.exit!==0){res.innerHTML='<div class="sr-e">'+esc(d.error||'Unknown error')+'</div>';return}
  if(d.sources&&d.sources.length){
    res.innerHTML='<div style="margin-bottom:8px;color:var(--ink);font-size:13px;font-weight:500">Pending sources:</div>'+
      d.sources.map(s=>'<div class="an"><div class="ant">'+esc(s)+'</div></div>').join('');
  } else {
    res.innerHTML='<div style="color:var(--green);font-size:13px;padding:10px 0">✓ Inbox is empty — nothing to ingest</div>';
  }
}

async function runBoard(){
  const q=document.getElementById('bd-q').value.trim();
  if(!q)return;
  const only=document.getElementById('bd-only').value.trim();
  const btn=document.getElementById('bd-btn'),status=document.getElementById('bd-status'),res=document.getElementById('bd-results');
  status.innerHTML='<span class="sp"></span> Consulting board...'; res.innerHTML='';
  btn.textContent='Running...'; btn.disabled=true;
  const d=await ap('/api/run/board','POST',{question:q,only:only});
  btn.textContent='Consult'; btn.disabled=false;
  if(d.error){
    status.textContent='Error'; res.innerHTML='<div class="sr-e">'+esc(d.error)+'</div>';
    return
  }
  status.textContent='Exit: '+d.exit+' · Question: '+esc(d.question)+(d.only?' · Advisors: '+esc(d.only):'');
  if(d.exit!==0){res.innerHTML='<div class="sr-e">'+esc(d.output||d.error||'Consult failed')+'</div>';return}
  if(!d.output||d.output.length<10){
    res.innerHTML='<div class="sr-e">Board returned no output</div>'; return
  }
  res.innerHTML='<div style="background:var(--surf);border:1px solid var(--border);border-radius:var(--rad-c);padding:14px 16px;font-size:13px;line-height:1.6;color:var(--dim)">'+escMd(d.output)+'</div>';
}

async function run(n){trk('run:'+n);const l=document.getElementById('ro-l'),t=document.getElementById('ro-t'),r=document.getElementById('ro'),rr=document.getElementById('ro-results');l.textContent=n;t.textContent='running...';r.style.display='flex';rr.style.display='none';rr.className='rr-hide';const d=await ap('/api/run/'+n,'POST');const out=d.output||d.error||'done';t.textContent=out.length>80?out.slice(0,77)+'...':out;rr.innerHTML='';if(out&&out[0]==='{')try{const j=JSON.parse(out);if(j.emails){rr.innerHTML=j.emails.map(function(e){var b=e.triage==='URGENT'?'rr-urgent':e.triage==='FYI'?'rr-fyi':'rr-noise';var l=e.triage==='URGENT'?'⚠ ':e.triage==='FYI'?'→ ':'· ';var s=e.source==='proton'?'[P]':'[G]';return '<div class=\"rr-card\"><span class=\"rr-badge '+b+'\">'+l+e.triage+'</span><div class=\"rr-body\"><div class=\"rr-subj\">'+esc(e.subject)+'<span class=\"rr-src\">'+s+'</span></div><div class=\"rr-from\">'+esc(e.from)+'</div><div class=\"rr-snip\">'+esc(e.snippet)+'</div></div></div>'}).join('');rr.className='';rr.style.display='block'}}catch(e){}setTimeout(refreshAll,3000)}
function runSkill(n){
  trk('skill:'+n);
  const m={triage:'inbox-triage',status:'event-bus-stats',gardener:'knowledge-gardener'};
  if(m[n])run(m[n]);
  else if(n==='recall'||n==='wiki'){document.getElementById('sr-q').value='';sw('search');document.getElementById('sr-q').placeholder='Search '+n+'...';setTimeout(()=>document.getElementById('sr-q').focus(),100)}
  else{const l=document.getElementById('ro-l'),t=document.getElementById('ro-t'),r=document.getElementById('ro');l.textContent=n;t.textContent='Type /'+n+' in the Hermes tab →';r.style.display='flex';st('hermes')}
}

// Collapsible All Skills — remembers state in localStorage
function toggleAllSkills(){
  const list=document.getElementById('all-sk-list'),arrow=document.getElementById('all-sk-arrow'),open=list.style.display!=='none';
  list.style.display=open?'none':'block';
  arrow.classList.toggle('open',!open);
  localStorage.setItem('dash-all-skills-open',!open?'1':'0');
}
(function(){
  const list=document.getElementById('all-sk-list'),arrow=document.getElementById('all-sk-arrow');
  if(localStorage.getItem('dash-all-skills-open')==='1'){list.style.display='block';arrow.classList.add('open')}
})();

refreshAll();setInterval(refreshAll,30000);
</script>
</body>
</html>"""

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    s = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Dashboard at http://{args.host}:{args.port}", file=sys.stderr)
    try: s.serve_forever()
    except KeyboardInterrupt: s.server_close()

if __name__ == "__main__":
    raise SystemExit(main())
