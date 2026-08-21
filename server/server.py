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
import json, os, re, shutil, threading, secrets, time, datetime, hashlib, hmac, base64
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
    # newline="\n" so an export run from a Windows dev machine doesn't rewrite
    # every line of the tracked bins.json with CRLF.
    with open(tmp, "w", newline="\n") as f:
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

# -------------------------------------------------------------- backups ----
KEEP_BACKUPS = 14

def prune_backups(keep=KEEP_BACKUPS):
    if not os.path.isdir(BACKUPS):
        return
    dirs = sorted(d for d in os.listdir(BACKUPS)
                  if os.path.isdir(os.path.join(BACKUPS, d)))
    for d in dirs[:-keep] if keep else dirs:
        shutil.rmtree(os.path.join(BACKUPS, d), ignore_errors=True)

def make_backup(reason="manual"):
    """Timestamped copy of every live file that has no git safety net.

    Called by the daily timer, and automatically immediately before any
    destructive admin action (handoff 6). Returns the directory it wrote.
    """
    ts   = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = os.path.join(BACKUPS, ts)
    os.makedirs(dest, exist_ok=True)
    for src in (USERS, STATE, OVERRIDES, ADDITIONS, AUDIT):
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dest, os.path.basename(src)))
    os.chmod(dest, 0o700)                 # copies include users.json
    write_json(os.path.join(dest, "reason.json"), {"reason": reason, "ts": now_iso()})
    prune_backups()
    return dest

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
    "manage_users":   "add/deactivate users, set permissions, reset PINs, unlock",
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

def public_user(u):
    """A user record safe to hand to a manage_users holder: everything except
    the PIN hash, salt and scrypt parameters."""
    out = {k: v for k, v in u.items() if k != "pin"}
    out["locked"] = _locked(u, time.time())
    return out

def clean_perms(raw):
    """Returns (perms, error). Unknown flags are rejected outright rather than
    silently dropped, so a typo in an admin call can't quietly grant nothing."""
    if not isinstance(raw, list):
        return None, "perms must be a list"
    perms = sorted({str(p) for p in raw})
    unknown = [p for p in perms if p not in PERMS]
    if unknown:
        return None, "unknown permission: " + ", ".join(unknown)
    return perms, None

def admins_left(users_doc, without_id=None, as_perms=None):
    """How many active users would still hold manage_users after a change."""
    n = 0
    for u in users_doc.get("users", []):
        perms  = u.get("perms") or []
        active = u.get("active", True)
        if u.get("id") == without_id:
            if as_perms is None:
                continue                       # being deactivated or deleted
            perms = as_perms
        if active and "manage_users" in perms:
            n += 1
    return n

def new_user_record(uid, name, pin, perms):
    return {"id": uid, "name": name, "pin": hash_pin(pin), "perms": perms,
            "active": True, "created": now_iso(), "last_login": None,
            "failed": 0, "locked_until": None}

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
        ip_locked = _locked(ipf, now)
        users = load_users()
        user  = find_user(users, badge_id) if RE_BADGE.match(badge_id or "") else None
        ok = False
        if user is not None and user.get("active", True) and not _locked(user, now):
            ok = verify_pin(pin, user.get("pin"))
        is_super = ok and "manage_users" in (user.get("perms") or [])

        # A super-user is never shut out by an IP lockout.
        #
        # The whole shop reaches this app through one Cloudflare-forwarded
        # address, so a single person fumbling their PIN five times locks
        # that address for everybody -- including the only people who can
        # clear it. So: correct credentials plus manage_users plus an
        # unlocked badge gets through an IP lock, and succeeding clears the
        # lock for everyone else too (see IP_FAILS.pop below). That is the
        # intended "let me back in and unlock the shop" path.
        #
        # Their own per-badge lockout still applies -- clear that from the
        # admin panel's Unlock button, or `adminctl.py unlock <badge>`.
        if ip_locked and not is_super:
            if not ok:
                # Re-arm the IP lock so hammering during a lock buys nothing,
                # but deliberately do NOT count this against the badge:
                # otherwise someone who locked the shared IP could go on to
                # lock out every badge in the shop one after another.
                ipf["locked_until"] = now + LOCKOUT_SECS
            return None

        if not ok:
            _note_failure(ipf, now)
            if user is not None:
                _note_failure(user, now)   # per-badge lockout, survives restart
                save_users(users)
            return None
        IP_FAILS.pop(ip, None)             # a good login clears the IP lock
        user["failed"] = 0
        user["locked_until"] = None
        user["last_login"] = now_iso()
        save_users(users)
        return user

def clear_ip_locks():
    """Drop every IP lockout. Behind the shared shop address there is really
    only one, and clearing it is the point of the admin Unlock action."""
    with _fail_lock:
        IP_FAILS.clear()

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

OVERRIDABLE = ("cab", "bin", "row", "col", "pn", "zone")

def with_overrides():
    """bins.json + additions.json, with overrides.json layered on top.

    Only the fields actually present in an override are applied; everything
    else falls through to the seed record. Records are copied first so the
    seed data on disk is never mutated in memory.
    """
    ov   = load_overrides().get("overrides", {})
    recs = [dict(r) for r in load_bins()["bins"]] + [dict(r) for r in load_additions()]
    for r in recs:
        o = ov.get(r.get("id"))
        if not o:
            continue
        for k in OVERRIDABLE:
            if k in o:
                r[k] = o[k]
        if o.get("note"):
            r["editNote"] = o["note"]        # distinct from the curated boxNote
        r["editedBy"] = o.get("edited_by")
        r["editedAt"] = o.get("edited_at")
    return recs

def merged():
    """Serve order: bins.json -> overrides.json -> live counts from state.json."""
    state = load_state()
    recs  = with_overrides()
    for r in recs:
        s = state.get(r["id"])
        if s:
            r["boxes"]   = s.get("boxes", r.get("boxes", 0))
            r["binFull"] = s.get("binFull", r.get("binFull", False))
    return {"version": load_bins()["version"], "count": len(recs), "bins": recs}

def all_ids():
    return {r["id"] for r in load_bins()["bins"]} | {r["id"] for r in load_additions()}

def bin_taken_by(cab, bin_code, ignore_id=None):
    """Which record id currently occupies this cabinet+bin, post-override.

    Checked against the *live* view, not the seed, so a bin freed up by an
    earlier edit is reusable and a bin an edit moved into is protected.
    """
    for r in with_overrides():
        if r.get("id") == ignore_id or r.get("cab") == "?":
            continue
        if r.get("cab") == cab and str(r.get("bin", "")).upper() == bin_code:
            return r["id"]
    return None

# No client string is trusted. Everything that reaches a record goes through
# one of these first; anything that doesn't match is a 400.
RE_PN   = re.compile(r"^[0-9A-Za-z][0-9A-Za-z./-]{1,31}$")
RE_BIN  = re.compile(r"^(?:[A-Z]{1,2}[0-9]{1,2}|[0-9]{1,2}-[0-9]{1,2})$")
RE_ID   = re.compile(r"^(?:[AB]-[A-Z0-9-]{1,6}|NB-[0-9A-Za-z]{1,8})$")
NOTE_MAX = 120

def clean_note(raw):
    """Strip control characters and cap the length. Notes are shown verbatim
    in the UI, which escapes them, but keep the stored value tidy regardless."""
    txt = "".join(c for c in str(raw or "") if c.isprintable()).strip()
    return txt[:NOTE_MAX]

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
        if path == "/api/admin/users":
            if not self._require("/api/admin/users"):
                return
            # PIN material is stripped by public_user(). The flag catalog is
            # included because only a manage_users holder can reach this.
            return self._json({"users": [public_user(u) for u in
                                         load_users().get("users", [])],
                               "perms": PERMS})
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
            if not RE_PN.match(pn):
                return self._json({"error": "Part number must be 2-32 letters, digits, "
                                            ". - or /"}, 400)
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
                    if not RE_BIN.match(bin_code):
                        return self._json({"error": f"couldn't parse bin \"{bin_code}\" — use e.g. N9 or 6-5"}, 400)
                    row, col = parse_bin_code(bin_code)
                    if row is None:
                        return self._json({"error": f"couldn't parse bin \"{bin_code}\" — use e.g. N9 or 6-5"}, 400)
                    rid = f"{cab}-{bin_code}"
                    holder = bin_taken_by(cab, bin_code)
                    if rid in all_ids() or holder:
                        return self._json({"error": f"bin C{cab}-{bin_code} is already assigned"
                                                    f" (record {holder or rid})"}, 409)
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
                audit(session, "add_part", rid, pn=pn, boxes=boxes,
                      cab=record["cab"], bin=record["bin"])
                return self._json({"ok": True, "bin": record})

        if path == "/api/bins/edit":
            rid = str(payload.get("id") or "").strip()
            if not RE_ID.match(rid):
                return self._json({"error": "bad record id"}, 400)
            want_cab  = payload.get("cab")
            want_bin  = payload.get("bin")
            want_pn   = payload.get("pn")
            note      = clean_note(payload.get("note"))
            if want_cab is None and want_bin is None and want_pn is None:
                return self._json({"error": "nothing to change"}, 400)

            with _lock:
                live = {r["id"]: r for r in with_overrides()}
                cur  = live.get(rid)
                if not cur:
                    return self._json({"error": "no such record"}, 404)

                patch = {}
                if want_pn is not None:
                    pn = str(want_pn).strip()
                    if not RE_PN.match(pn):
                        return self._json({"error": "Part number must be 2-32 letters, "
                                                    "digits, . - or /"}, 400)
                    patch["pn"] = pn

                # cab and bin move together: a bin code means nothing without
                # the cabinet it lives in.
                if want_cab is not None or want_bin is not None:
                    cab = str(want_cab if want_cab is not None else cur.get("cab") or "").strip().upper()
                    bin_code = str(want_bin if want_bin is not None else cur.get("bin") or "").strip().upper()
                    if cab not in ("A", "B"):
                        return self._json({"error": "pick cabinet A or B"}, 400)
                    if not RE_BIN.match(bin_code):
                        return self._json({"error": f"couldn't parse bin \"{bin_code}\" — "
                                                     "use e.g. N9 or 6-5"}, 400)
                    row, col = parse_bin_code(bin_code)
                    if row is None:
                        return self._json({"error": f"couldn't parse bin \"{bin_code}\" — "
                                                     "use e.g. N9 or 6-5"}, 400)
                    holder = bin_taken_by(cab, bin_code, ignore_id=rid)
                    if holder:
                        other = live.get(holder, {})
                        return self._json({"error": f"C{cab}-{bin_code} already holds "
                                                    f"{other.get('pn', holder)}. Move or fix that "
                                                    "entry first."}, 409)
                    # row/col/zone are always derived here, never taken from
                    # the client (handoff 5.1).
                    patch.update({"cab": cab, "bin": bin_code, "row": row, "col": col,
                                  "zone": compute_zone(cab, row)})

                before = {k: cur.get(k) for k in patch}
                doc = load_overrides()
                entry = doc.setdefault("overrides", {}).setdefault(rid, {})
                entry.update(patch)
                if note:
                    entry["note"] = note
                entry["edited_by"] = session["id"]
                entry["edited_at"] = now_iso()
                # The record id is NOT re-keyed when cab/bin change. It stays
                # the original stable key forever: it is only an identifier,
                # and state.json counts plus this override map are both keyed
                # off it. Re-keying would orphan a bin's box count. Do not
                # "fix" this later.
                save_overrides(doc)
                audit(session, "edit_bin", rid, before=before, after=patch,
                      note=note or None)
                merged_rec = {r["id"]: r for r in merged()["bins"]}.get(rid)
                return self._json({"ok": True, "bin": merged_rec})

        if path == "/api/admin/users":
            action = str(payload.get("action") or "").strip().lower()
            uid    = str(payload.get("id") or "").strip()
            if not RE_BADGE.match(uid):
                return self._json({"error": "Badge number must be 1-12 digits."}, 400)

            with _lock:
                users = load_users()
                target = find_user(users, uid)

                if action == "create":
                    if target:
                        return self._json({"error": f"Badge {uid} already exists."}, 409)
                    name = clean_note(payload.get("name"))
                    if not name:
                        return self._json({"error": "Name is required."}, 400)
                    perms, err = clean_perms(payload.get("perms") or ["view"])
                    if err:
                        return self._json({"error": err}, 400)
                    pin = str(payload.get("pin") or "")
                    problem = pin_problem(pin, uid)
                    if problem:
                        return self._json({"error": problem}, 400)
                    rec = new_user_record(uid, name, pin, perms)
                    users.setdefault("users", []).append(rec)
                    save_users(users)
                    # perms are logged, the PIN never is
                    audit(session, "user_create", uid, name=name, perms=perms)
                    return self._json({"ok": True, "user": public_user(rec)})

                if not target:
                    return self._json({"error": "No such badge number."}, 404)

                if action == "update":
                    before = {"name": target.get("name"), "perms": target.get("perms"),
                              "active": target.get("active", True)}
                    if payload.get("name") is not None:
                        name = clean_note(payload.get("name"))
                        if not name:
                            return self._json({"error": "Name is required."}, 400)
                        target["name"] = name
                    if payload.get("perms") is not None:
                        perms, err = clean_perms(payload.get("perms"))
                        if err:
                            return self._json({"error": err}, 400)
                        if uid == session["id"] and "manage_users" not in perms:
                            return self._json({"error": "You cannot remove your own "
                                                        "user-management access."}, 400)
                        if admins_left(users, without_id=uid, as_perms=perms) == 0:
                            return self._json({"error": "That would leave nobody able to "
                                                        "manage users."}, 400)
                        target["perms"] = perms
                    if payload.get("active") is not None:
                        want = bool(payload.get("active"))
                        if not want:
                            if uid == session["id"]:
                                return self._json({"error": "You cannot deactivate your "
                                                            "own account."}, 400)
                            if admins_left(users, without_id=uid) == 0:
                                return self._json({"error": "That would leave nobody able "
                                                            "to manage users."}, 400)
                            make_backup(f"deactivate {uid}")
                        target["active"] = want
                    save_users(users)
                    audit(session, "user_update", uid, before=before,
                          after={"name": target.get("name"), "perms": target.get("perms"),
                                 "active": target.get("active", True)})
                    return self._json({"ok": True, "user": public_user(target)})

                if action == "unlock":
                    before = {"failed": target.get("failed", 0),
                              "locked_until": target.get("locked_until")}
                    target["failed"] = 0
                    target["locked_until"] = None
                    save_users(users)
                    # Also drop IP lockouts: the shared shop address is the
                    # usual reason anyone is locked out in the first place.
                    clear_ip_locks()
                    audit(session, "user_unlock", uid, before=before)
                    return self._json({"ok": True, "user": public_user(target)})

                if action in ("deactivate", "delete"):
                    if uid == session["id"]:
                        return self._json({"error": "You cannot deactivate or delete your "
                                                    "own account."}, 400)
                    if admins_left(users, without_id=uid) == 0:
                        return self._json({"error": "That would leave nobody able to "
                                                    "manage users."}, 400)
                    make_backup(f"{action} {uid}")
                    if action == "deactivate":
                        # Preferred: keeps the record so audit.log attribution
                        # still resolves to a name.
                        target["active"] = False
                        save_users(users)
                    else:
                        users["users"] = [u for u in users["users"] if u.get("id") != uid]
                        save_users(users)
                    audit(session, "user_" + action, uid, name=target.get("name"))
                    return self._json({"ok": True})

                return self._json({"error": "action must be create, update, "
                                            "unlock, deactivate or delete"}, 400)

        if path == "/api/admin/users/pin":
            uid = str(payload.get("id") or "").strip()
            pin = str(payload.get("pin") or "")
            if not RE_BADGE.match(uid):
                return self._json({"error": "Badge number must be 1-12 digits."}, 400)
            problem = pin_problem(pin, uid)
            if problem:
                return self._json({"error": problem}, 400)
            with _lock:
                users  = load_users()
                target = find_user(users, uid)
                if not target:
                    return self._json({"error": "No such badge number."}, 404)
                make_backup(f"pin reset {uid}")
                target["pin"] = hash_pin(pin)
                target["failed"] = 0            # a reset also clears a lockout
                target["locked_until"] = None
                save_users(users)
                audit(session, "user_pin_reset", uid)   # never the PIN itself
                return self._json({"ok": True})

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
    # Fail at startup, not on the first login attempt: a server that boots
    # without a signing key looks healthy and then turns every login into a
    # 500 at the worst possible moment.
    session_key()
    if not load_users().get("users"):
        print("WARNING: no users yet -- nobody can log in.")
        print("         Run: python3 server/adminctl.py bootstrap")
    print(f"Bolt Cabinet Lookup - serving {PUBLIC} on http://0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
