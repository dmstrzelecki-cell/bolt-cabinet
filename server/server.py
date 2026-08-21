#!/usr/bin/env python3
"""Bolt Cabinet Lookup - T320 server (Python stdlib only, no deps).

Serves the static app in ../public and persists live back-stock counts in
../state.json (separate from the curated bins.json so inventory edits stay
git-clean). Run:  python3 server/server.py   (PORT env overrides :8080)

Live API access (/api/bins and writes) requires a session established via
POST /api/login {pin} against ../employees.json (gitignored, T320-only —
maps each employee's 6-digit PIN to a name + role). Static files (index.html,
images) are served unauthenticated so the login screen itself can load.
Every box/bin-full write is appended to ../audit.log (gitignored, JSONL) with
who made the change, what bin, and the before/after values.

New parts added from the app (POST /api/parts) land in ../additions.json
(gitignored, T320-only) rather than the curated, git-tracked bins.json --
merged() combines both so the two never have to be reconciled by hand, and
the container's data never drifts from what's in git.
"""
import json, os, re, threading, secrets, time, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import urlparse

ROOT      = os.path.dirname(os.path.abspath(__file__))
APPDIR    = os.path.normpath(os.path.join(ROOT, ".."))
PUBLIC    = os.path.join(APPDIR, "public")
BINS      = os.path.join(PUBLIC, "bins.json")
STATE     = os.path.join(APPDIR, "state.json")
ADDITIONS = os.path.join(APPDIR, "additions.json")
OVERRIDES = os.path.join(APPDIR, "overrides.json")
USERS     = os.path.join(APPDIR, "users.json")
EMPLOYEES = os.path.join(APPDIR, "employees.json")   # legacy, retired in Stage 2
AUDIT     = os.path.join(APPDIR, "audit.log")
ENVFILE   = os.path.join(APPDIR, ".env")
BACKUPS   = os.path.join(APPDIR, "backups")
PORT      = int(os.environ.get("PORT", "8080"))
_lock     = threading.Lock()

# ---------------------------------------------------------------- .env -----
# Secrets live in a gitignored ../.env generated on the container (see README).
# Nothing in here is ever logged, echoed, or returned to a client.
def load_env(path=ENVFILE):
    env = {}
    if not os.path.exists(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env

ENV = load_env()

def session_key():
    """Raise loudly rather than silently signing sessions with a guessable key."""
    key = os.environ.get("BOLT_SESSION_KEY") or ENV.get("BOLT_SESSION_KEY")
    if not key:
        raise SystemExit(f"""FATAL: BOLT_SESSION_KEY is not set -- sessions cannot be signed.
  Generate one on this machine and write it to {ENVFILE}:
    python3 -c 'import secrets;print("BOLT_SESSION_KEY="+secrets.token_urlsafe(48))'
  then chmod 600 that file. See README.
  Never commit it, print it, or paste its contents anywhere.""")
    return key.encode()

# ----------------------------------------------------------- json store ----
def write_json(path, obj, mode=None):
    """Atomic write: full temp file, fsync, then rename over the target, so a
    crash or a yanked power cord mid-write can never leave a truncated file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)

def read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)

def load_users():
    return read_json(USERS, {"version": 1, "users": []})

def save_users(doc):
    write_json(USERS, doc, mode=0o600)   # PIN hashes -- owner-only

def load_overrides():
    return read_json(OVERRIDES, {"version": 1, "overrides": {}})

def save_overrides(doc):
    write_json(OVERRIDES, doc)

def audit(employee_id, name, bin_id, action, **fields):
    entry = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
              "employeeId": employee_id, "name": name, "binId": bin_id, "action": action}
    entry.update(fields)
    with open(AUDIT, "a") as f:
        f.write(json.dumps(entry) + "\n")

SESSIONS      = {}                 # token -> {"name","role","created"}
SESSION_TTL   = 12 * 3600          # a work shift
_fail_lock    = threading.Lock()
FAILS         = {}                 # client key -> {"count","locked_until"}
MAX_FAILS     = 5
LOCKOUT_SECS  = 5 * 60

def load_employees():
    if not os.path.exists(EMPLOYEES):
        return {}
    with open(EMPLOYEES) as f: return json.load(f)

CTYPES = {"html":"text/html","json":"application/json","js":"text/javascript",
          "css":"text/css","jpg":"image/jpeg","jpeg":"image/jpeg",
          "png":"image/png","webp":"image/webp","txt":"text/plain"}

def load_bins():
    with open(BINS) as f: return json.load(f)

def load_additions():
    return read_json(ADDITIONS, [])

def save_additions(items):
    write_json(ADDITIONS, items)

def save_state(state):
    write_json(STATE, state)

def load_state():
    if os.path.exists(STATE):
        return read_json(STATE, {})
    state = {}                       # first run: seed from curated bins.json
    for r in load_bins()["bins"]:
        if r.get("boxes") is not None or r.get("binFull") is not None:
            state[r["id"]] = {"boxes": int(r.get("boxes", 0)),
                              "binFull": bool(r.get("binFull", False))}
    save_state(state); return state

def merged():
    data = load_bins(); state = load_state()
    bins = list(data["bins"]) + load_additions()
    for r in bins:
        s = state.get(r["id"])
        if s:
            r["boxes"]   = s.get("boxes", r.get("boxes", 0))
            r["binFull"] = s.get("binFull", r.get("binFull", False))
    return {"version": data["version"], "count": len(bins), "bins": bins}

def all_ids():
    return {r["id"] for r in load_bins()["bins"]} | {r["id"] for r in load_additions()}

# Mirrors the physical addressing scheme in CLAUDE.md.
def parse_bin_code(code):
    m = re.match(r'^(\d+)-(\d+)$', code)          # CB numeric block, e.g. "6-1"
    if m: return m.group(1), m.group(2)
    m = re.match(r'^([A-Za-z]+)(\d+)$', code)     # letter row, e.g. "N8"
    if m: return m.group(1).upper(), m.group(2)
    return None, None

def compute_zone(cab, row):
    if cab == "A":
        if row in ("A", "B", "C"): return "Left Door"
        if row in ("M", "N", "O", "P", "Q", "R", "S", "T"): return "Right Door"
        return "Main"                              # D-L
    if cab == "B":
        if row.isdigit() or row in ("A","B","C","D","E","F","G","H","I"): return "Left Door"
        if row in ("S", "T", "U", "V", "W", "X", "Y", "Z"): return "Right Door"
        return "Main"                              # J-R
    return None

class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200, extra_headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)

    def _client_key(self):
        # Cloudflare Tunnel funnels every visitor through one local
        # connection, so the raw socket peer is useless for rate-limiting -
        # prefer the header Cloudflare sets to the real client IP.
        return (self.headers.get("CF-Connecting-IP")
                or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
                or self.client_address[0])

    def _cookie(self, name):
        raw = self.headers.get("Cookie")
        if not raw: return None
        c = SimpleCookie(); c.load(raw)
        return c[name].value if name in c else None

    def _session(self):
        tok = self._cookie("session")
        if not tok: return None
        s = SESSIONS.get(tok)
        if not s: return None
        if time.time() - s["created"] > SESSION_TTL:
            SESSIONS.pop(tok, None)
            return None
        return s

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/session":
            s = self._session()
            if s: return self._json({"authenticated": True, "name": s["name"], "role": s["role"]})
            return self._json({"authenticated": False})
        if path == "/api/bins":
            if not self._session():
                return self._json({"error": "unauthorized"}, 401)
            return self._json(merged())
        rel = path.lstrip("/") or "index.html"
        fp  = os.path.normpath(os.path.join(PUBLIC, rel))
        if not fp.startswith(PUBLIC) or not os.path.isfile(fp):
            return self.send_error(404)
        ext = fp.rsplit(".", 1)[-1].lower()
        with open(fp, "rb") as f: body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", CTYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        path   = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json({"error": "bad json"}, 400)

        if path == "/api/login":
            key = self._client_key()
            now = time.time()
            with _fail_lock:
                f = FAILS.get(key)
                if f and f["locked_until"] > now:
                    return self._json({"ok": False, "error": "locked",
                                        "retryAfter": int(f["locked_until"] - now)}, 429)
            pin = str(payload.get("pin", "")).strip()
            person = load_employees().get(pin)
            if not person:
                with _fail_lock:
                    f = FAILS.setdefault(key, {"count": 0, "locked_until": 0})
                    f["count"] += 1
                    if f["count"] >= MAX_FAILS:
                        f["locked_until"] = now + LOCKOUT_SECS
                        f["count"] = 0
                return self._json({"ok": False, "error": "invalid pin"}, 401)
            with _fail_lock:
                FAILS.pop(key, None)
            token = secrets.token_urlsafe(32)
            name  = person.get("name", "Employee")
            role  = person.get("role", "editor")
            SESSIONS[token] = {"employeeId": pin, "name": name, "role": role, "created": now}
            cookie = f"session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}"
            return self._json({"ok": True, "name": name, "role": role},
                               extra_headers={"Set-Cookie": cookie})

        if path == "/api/logout":
            tok = self._cookie("session")
            if tok: SESSIONS.pop(tok, None)
            return self._json({"ok": True},
                               extra_headers={"Set-Cookie": "session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})

        session = self._session()
        if not session:
            return self._json({"error": "unauthorized"}, 401)

        if path == "/api/parts":
            if session["role"] != "editor":
                return self._json({"error": "forbidden"}, 403)
            pn = str(payload.get("pn", "")).strip()
            if not pn:
                return self._json({"error": "part number required"}, 400)
            cab = str(payload.get("cab") or "").strip().upper()
            bin_code = str(payload.get("bin") or "").strip().upper()
            try:
                boxes = max(0, int(payload.get("boxes", 1)))
            except (TypeError, ValueError):
                return self._json({"error": "boxes must be a number"}, 400)

            with _lock:
                if bin_code:
                    if cab not in ("A", "B"):
                        return self._json({"error": "pick a cabinet for that bin"}, 400)
                    row, col = parse_bin_code(bin_code)
                    if row is None:
                        return self._json({"error": f"couldn't parse bin \"{bin_code}\" — use e.g. N9 or 6-5"}, 400)
                    rid = f"{cab}-{bin_code}"
                    if rid in all_ids():
                        return self._json({"error": f"bin {cab}-{bin_code} is already assigned"}, 409)
                    record = {"cab": cab, "bin": bin_code, "row": row, "col": col, "pn": pn,
                              "zone": compute_zone(cab, row), "verify": False,
                              "boxes": boxes, "binFull": False, "id": rid}
                else:
                    last4 = pn[-4:]
                    rid = f"NB-{last4}"
                    if rid in all_ids():
                        return self._json({"error": f"a no-bin item ending in {last4} already exists — "
                                                      "give this one a bin location instead"}, 409)
                    record = {"cab": "?", "bin": "—", "row": "", "col": "", "pn": pn,
                              "zone": "Back stock — no bin", "verify": False,
                              "boxes": boxes, "binFull": False, "id": rid}
                additions = load_additions()
                additions.append(record)
                save_additions(additions)
                state = load_state()
                state[rid] = {"boxes": boxes, "binFull": False}
                save_state(state)
                audit(session["employeeId"], session["name"], rid, "add_part", pn=pn, boxes=boxes)
                return self._json({"ok": True, "bin": record})

        rid = payload.get("id")
        if not rid:
            return self._json({"error": "missing id"}, 400)
        if path in ("/api/box", "/api/binfull") and session["role"] != "editor":
            return self._json({"error": "forbidden"}, 403)
        with _lock:
            state = load_state()
            entry = state.setdefault(rid, {"boxes": 0, "binFull": False})
            if path == "/api/box":
                delta = int(payload.get("delta", 0))
                before = int(entry.get("boxes", 0))
                entry["boxes"] = max(0, before + delta)
                audit(session["employeeId"], session["name"], rid, "box",
                      delta=delta, before=before, after=entry["boxes"])
            elif path == "/api/binfull":
                before = bool(entry.get("binFull", False))
                entry["binFull"] = bool(payload.get("value"))
                audit(session["employeeId"], session["name"], rid, "binfull",
                      before=before, after=entry["binFull"])
            else:
                return self._json({"error": "unknown endpoint"}, 404)
            save_state(state)
            return self._json({"id": rid, **entry})

    def log_message(self, *a):        # keep the console quiet
        pass

if __name__ == "__main__":
    print(f"Bolt Cabinet Lookup - serving {PUBLIC} on http://0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
