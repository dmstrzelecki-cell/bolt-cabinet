#!/usr/bin/env python3
"""Bolt Cabinet Lookup - T320 server (Python stdlib only, no deps).

Serves the static app in ../public and owns every piece of live state that
must not live in git. Run:  python3 server/server.py   (PORT env overrides :8080)

Auth: each user has a badge id + a PIN, stored in ../users.json with the PIN
scrypt-hashed (never plaintext, never logged). POST /api/login {id, pin}
returns an HMAC-signed session cookie; the cookie carries only a user id and
an issue time, and permissions are re-read from users.json on every request.
Every mutating endpoint re-checks the caller's permission server-side --
hiding a button in the UI is cosmetic, this is the actual boundary.

Server-owned, gitignored files, all beside this repo in ../:
  .env            BOLT_SESSION_KEY, generated on the container
  users.json      badge id -> name, PIN hash, permission flags
  state.json      live box counts / binFull, keyed by record id
  overrides.json  bin/cabinet/part-number edits overlaying bins.json
  additions.json  brand-new records added from the app
  audit.log       JSONL, one line per change: who, what, before/after
  backups/        timestamped copies (see adminctl.py)

bins.json stays a clean, git-tracked seed that the server never writes to --
a dirty working tree would make `git pull --ff-only` fail on the container
and block every future deploy. merged() layers overrides and counts on top
of it at serve time instead.
"""
import json, os, re, threading, secrets, time, datetime, hashlib, hmac, base64
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

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

def audit(user, action, target_id=None, **fields):
    """Append-only attribution log. One JSON object per line. Never contains
    PIN material -- see hash_pin(); only ids, names, and before/after values."""
    entry = {"ts": now_iso(),
             "user_id": (user or {}).get("id"),
             "name":    (user or {}).get("name"),
             "action":  action,
             "target_id": target_id}
    entry.update(fields)
    with open(AUDIT, "a") as f:
        f.write(json.dumps(entry) + "\n")

# --------------------------------------------------------- permissions ----
# The whole permission vocabulary lives here and nowhere else. Swapping the
# flag set is an edit to these two tables plus the UI's own copy of the names.
# "manage_users" implies nothing else -- every flag is granted explicitly.
PERMS = {
    "view":           "log in, search, see bins and counts",
    "adjust_counts":  "+ received / - to bin",
    "toggle_binfull":  "the Bin-full toggle",
    "edit_bins":      "change bin location / cabinet / part number on an entry",
    "add_parts":      "create a new entry, including NB-<last4> back stock",
    "manage_users":   "add/deactivate users, set permissions, reset PINs",
}

# Every mutating route must appear here. A route with no entry is refused
# outright rather than defaulting open -- see _require().
ROUTE_PERMS = {
    "/api/bins":            "view",
    "/api/box":             "adjust_counts",
    "/api/binfull":         "toggle_binfull",
    "/api/bins/edit":       "edit_bins",
    "/api/bins/add":        "add_parts",
    "/api/parts":           "add_parts",     # legacy alias of /api/bins/add
    "/api/admin/users":     "manage_users",
    "/api/admin/users/pin": "manage_users",
}

# ---------------------------------------------------------------- auth -----
SESSION_TTL  = 12 * 3600           # a work shift
MAX_FAILS    = 5                   # per badge id AND per source IP
LOCKOUT_SECS = 15 * 60
SCRYPT_N, SCRYPT_R, SCRYPT_P = 16384, 8, 1

RE_BADGE = re.compile(r"^[0-9]{1,12}$")
RE_PIN   = re.compile(r"^[0-9]{6,32}$")

_fail_lock = threading.Lock()
IP_FAILS   = {}                    # source ip -> {"count", "locked_until"}

def _b64(raw):  return base64.urlsafe_b64encode(raw).decode().rstrip("=")
def _unb64(s):  return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def hash_pin(pin):
    """scrypt with a fresh per-user salt. The PIN itself is never stored."""
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pin.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                       p=SCRYPT_P, dklen=32)
    return {"algo": "scrypt", "salt": _b64(salt), "hash": _b64(h),
            "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P}

def verify_pin(pin, rec):
    if not isinstance(rec, dict) or rec.get("algo") != "scrypt":
        return False
    try:
        expect = _unb64(rec["hash"])
        got = hashlib.scrypt(pin.encode(), salt=_unb64(rec["salt"]),
                             n=int(rec["n"]), r=int(rec["r"]), p=int(rec["p"]),
                             dklen=len(expect))
    except Exception:
        return False
    return hmac.compare_digest(got, expect)

def pin_problem(pin, badge_id):
    """Returns a human message if the PIN is too weak to accept, else None.
    Used at bootstrap / add-user / reset time -- never during login."""
    if not RE_PIN.match(pin or ""):
        return "PIN must be 6 or more digits, digits only."
    if len(set(pin)) == 1:
        return "PIN cannot be all the same digit."
    runs = all(int(pin[i + 1]) - int(pin[i]) == 1 for i in range(len(pin) - 1))
    back = all(int(pin[i]) - int(pin[i + 1]) == 1 for i in range(len(pin) - 1))
    if runs or back:
        return "PIN cannot be a run of consecutive digits."
    if pin == badge_id:
        return "PIN cannot be the same as your badge number."
    return None

def has(user, perm):
    """The one place a permission is checked. Server-side only -- the UI
    hiding a button is cosmetic; this is the actual boundary."""
    return bool(user) and perm in (user.get("perms") or [])

def find_user(users_doc, uid):
    for u in users_doc.get("users", []):
        if u.get("id") == uid:
            return u
    return None

# Sessions are stateless: a signed cookie carrying only the user id and an
# issue time. Permissions are re-read from users.json on every request, so
# deactivating someone or changing their flags takes effect immediately and
# survives a service restart -- no in-memory session table to go stale.
def issue_session(uid):
    body = _b64(json.dumps({"u": uid, "iat": int(time.time())},
                           separators=(",", ":")).encode())
    sig = _b64(hmac.new(session_key(), body.encode(), hashlib.sha256).digest())
    return body + "." + sig

def read_session(token):
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    expect = _b64(hmac.new(session_key(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expect):      # verify before trusting a field
        return None
    try:
        data = json.loads(_unb64(body))
        iat = int(data["iat"])
    except Exception:
        return None
    if time.time() - iat > SESSION_TTL:
        return None
    return data

def session_cookie(token=None):
    if token is None:
        return "session=; Path=/; HttpOnly; SameSite=Lax; Secure; Max-Age=0"
    return (f"session={token}; Path=/; HttpOnly; SameSite=Lax; Secure; "
            f"Max-Age={SESSION_TTL}")

def _locked(entry, now):
    return bool(entry) and float(entry.get("locked_until") or 0) > now

def _note_failure(entry, now):
    """Bump a failure counter and lock it once MAX_FAILS is reached."""
    entry["failed"] = int(entry.get("failed", 0)) + 1
    if entry["failed"] >= MAX_FAILS:
        entry["locked_until"] = now + LOCKOUT_SECS
        entry["failed"] = 0
    return entry

def authenticate(badge_id, pin, ip):
    """Returns the user record on success, None on any failure.

    Callers must not distinguish the failure reasons to the client: unknown
    badge, wrong PIN, locked out and deactivated all look identical from
    outside, so the endpoint cannot be used to enumerate valid badge numbers.
    """
    now = time.time()
    with _fail_lock:
        ipf = IP_FAILS.setdefault(ip, {"failed": 0, "locked_until": 0})
        if _locked(ipf, now):
            return None
        users = load_users()
        user  = find_user(users, badge_id) if RE_BADGE.match(badge_id or "") else None
        ok = False
        if user is not None and user.get("active", True) and not _locked(user, now):
            ok = verify_pin(pin, user.get("pin"))
        if not ok:
            _note_failure(ipf, now)
            if user is not None:
                _note_failure(user, now)   # per-badge lockout, survives restart
                save_users(users)
            return None
        IP_FAILS.pop(ip, None)
        user["failed"] = 0
        user["locked_until"] = None
        user["last_login"] = now_iso()
        save_users(users)
        return user

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

    def _user(self):
        """The logged-in user record, re-read from users.json every request."""
        data = read_session(self._cookie("session"))
        if not data:
            return None
        u = find_user(load_users(), data.get("u"))
        if not u or not u.get("active", True):
            return None
        return u

    def _require(self, path):
        """Gate a route. Returns the user, or sends the error and returns None.

        This is the security boundary, not the UI. Anyone can POST here
        directly, so the check happens on every request regardless of what
        buttons the caller's browser rendered.
        """
        perm = ROUTE_PERMS.get(path)
        if perm is None:                     # unknown route: refuse, never allow
            self._json({"error": "unknown endpoint"}, 404)
            return None
        u = self._user()
        if not u:
            self._json({"error": "unauthorized"}, 401)
            return None
        if not has(u, perm):
            # Don't name the missing flag -- the client is only ever told its
            # own effective permissions, never the rest of the catalog.
            audit(u, "denied", path)
            self._json({"error": "forbidden"}, 403)
            return None
        return u

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/api/me", "/api/session"):
            # Only ever the caller's own record -- never other users, never
            # the permission catalog, never any PIN material.
            u = self._user()
            if u:
                return self._json({"authenticated": True, "id": u["id"],
                                   "name": u.get("name", ""),
                                   "perms": sorted(u.get("perms", []))})
            return self._json({"authenticated": False})
        if path == "/api/bins":
            if not self._require("/api/bins"):
                return
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
            badge = str(payload.get("id", "")).strip()
            pin   = str(payload.get("pin", "")).strip()
            user  = authenticate(badge, pin, self._client_key())
            if not user:
                # One message for every failure mode. Do not add a reason, a
                # retry-after, or a different status code here: any of those
                # turns this endpoint into a badge-number oracle.
                audit(None, "login_failed", badge or None, ip=self._client_key())
                return self._json({"ok": False,
                                   "error": "Badge number or PIN is incorrect."}, 401)
            audit(user, "login", user["id"])
            return self._json({"ok": True, "id": user["id"],
                               "name": user.get("name", ""),
                               "perms": sorted(user.get("perms", []))},
                              extra_headers={"Set-Cookie":
                                             session_cookie(issue_session(user["id"]))})

        if path == "/api/logout":
            return self._json({"ok": True},
                              extra_headers={"Set-Cookie": session_cookie(None)})

        session = self._require(path)
        if not session:
            return

        if path in ("/api/bins/add", "/api/parts"):
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
                audit(session, "add_part", rid, pn=pn, boxes=boxes)
                return self._json({"ok": True, "bin": record})

        rid = payload.get("id")
        if not rid:
            return self._json({"error": "missing id"}, 400)
        with _lock:
            state = load_state()
            entry = state.setdefault(rid, {"boxes": 0, "binFull": False})
            if path == "/api/box":
                delta = int(payload.get("delta", 0))
                before = int(entry.get("boxes", 0))
                entry["boxes"] = max(0, before + delta)
                audit(session, "box", rid,
                      delta=delta, before=before, after=entry["boxes"])
            elif path == "/api/binfull":
                before = bool(entry.get("binFull", False))
                entry["binFull"] = bool(payload.get("value"))
                audit(session, "binfull", rid,
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
