#!/usr/bin/env python3
"""Bolt Cabinet Lookup - T320 server (Python stdlib only, no deps).

Serves the static app in ../public and persists live back-stock counts in
../state.json (separate from the curated bins.json so inventory edits stay
git-clean). Run:  python3 server/server.py   (PORT env overrides :8080)
"""
import json, os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT   = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.normpath(os.path.join(ROOT, "..", "public"))
BINS   = os.path.join(PUBLIC, "bins.json")
STATE  = os.path.normpath(os.path.join(ROOT, "..", "state.json"))
PORT   = int(os.environ.get("PORT", "8080"))
_lock  = threading.Lock()

CTYPES = {"html":"text/html","json":"application/json","js":"text/javascript",
          "css":"text/css","jpg":"image/jpeg","jpeg":"image/jpeg",
          "png":"image/png","webp":"image/webp","txt":"text/plain"}

def load_bins():
    with open(BINS) as f: return json.load(f)

def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f: json.dump(state, f, indent=1)
    os.replace(tmp, STATE)          # atomic write

def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f: return json.load(f)
    state = {}                       # first run: seed from curated bins.json
    for r in load_bins()["bins"]:
        if r.get("boxes") is not None or r.get("binFull") is not None:
            state[r["id"]] = {"boxes": int(r.get("boxes", 0)),
                              "binFull": bool(r.get("binFull", False))}
    save_state(state); return state

def merged():
    data = load_bins(); state = load_state()
    for r in data["bins"]:
        s = state.get(r["id"])
        if s:
            r["boxes"]   = s.get("boxes", r.get("boxes", 0))
            r["binFull"] = s.get("binFull", r.get("binFull", False))
    return data

class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/bins":
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
        rid = payload.get("id")
        if not rid:
            return self._json({"error": "missing id"}, 400)
        with _lock:
            state = load_state()
            entry = state.setdefault(rid, {"boxes": 0, "binFull": False})
            if path == "/api/box":
                entry["boxes"] = max(0, int(entry.get("boxes", 0)) + int(payload.get("delta", 0)))
            elif path == "/api/binfull":
                entry["binFull"] = bool(payload.get("value"))
            else:
                return self._json({"error": "unknown endpoint"}, 404)
            save_state(state)
            return self._json({"id": rid, **entry})

    def log_message(self, *a):        # keep the console quiet
        pass

if __name__ == "__main__":
    print(f"Bolt Cabinet Lookup - serving {PUBLIC} on http://0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
