#!/usr/bin/env python3
"""Print the live back-stock list.

Read-only. Never writes state.json, bins.json or additions.json.

Two ways to get the data, in order of preference:

  1. Local join (default) - reads bins.json + additions.json + state.json off
     disk and overlays them exactly the way server.py's merged() does. No PIN
     needed, works whether or not the service is up.

  2. Live API - pass --pin 123456 to log in and read /api/bins over HTTP.
     /api/bins requires a session cookie, so a bare curl gets 401; this does
     the POST /api/login handshake first. Use it when you want to confirm the
     running process agrees with what's on disk.

Usage on the T320:
    python3 /opt/bolt-cabinet/server/backstock_report.py
    python3 /opt/bolt-cabinet/server/backstock_report.py --pin 123456
"""

import argparse
import json
import os
import sys

ROOT      = os.path.dirname(os.path.abspath(__file__))
PUBLIC    = os.path.join(ROOT, "..", "public")
BINS      = os.path.normpath(os.path.join(PUBLIC, "bins.json"))
ADDITIONS = os.path.normpath(os.path.join(ROOT, "..", "additions.json"))
STATE     = os.path.normpath(os.path.join(ROOT, "..", "state.json"))


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def from_disk():
    """Same overlay server.py's merged() does, but read-only.

    merged() calls load_state(), which seeds and writes state.json on first
    run. We deliberately don't - a reporting tool must not create live state.
    """
    data = load_json(BINS, {"version": "?", "bins": []})
    bins = list(data["bins"]) + load_json(ADDITIONS, [])
    state = load_json(STATE, None)
    if state is None:
        return bins, data.get("version"), False
    for r in bins:
        s = state.get(r["id"])
        if s:
            r["boxes"]   = s.get("boxes", r.get("boxes", 0))
            r["binFull"] = s.get("binFull", r.get("binFull", False))
    return bins, data.get("version"), True


def from_api(base, pin):
    import urllib.request
    import urllib.error

    def post(path, payload):
        req = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=10)

    try:
        resp = post("/api/login", {"pin": pin})
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        sys.exit(f"login failed: HTTP {e.code} {body}")
    cookie = resp.headers.get("Set-Cookie", "").split(";")[0]
    if not cookie:
        sys.exit("login returned no session cookie")

    req = urllib.request.Request(base + "/api/bins", headers={"Cookie": cookie})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"/api/bins failed: HTTP {e.code}")
    return data["bins"], data.get("version"), True


def is_backstock(r):
    return bool(
        r.get("boxes")
        or r.get("binFull")
        or r.get("zone") == "Back stock — no bin"
        or r.get("cab") == "?"
    )


def sort_key(r):
    """No-bin entries first, then cabinet, then bin (row letter, numeric col)."""
    nobin = r.get("cab") == "?" or str(r.get("id", "")).startswith("NB-")
    if nobin:
        return (0, "", "", 0, r.get("pn", ""))
    row = str(r.get("row") or "")
    col = str(r.get("col") or "")
    # Cabinet B has a numeric block (rows 1-6) below the letters; sort those
    # after the lettered rows rather than interleaving them as strings.
    row_key = ("0" + row) if row.isalpha() else ("1" + row.zfill(3))
    return (1, str(r.get("cab") or ""), row_key,
            int(col) if col.isdigit() else 0, r.get("pn", ""))


def location(r):
    if r.get("cab") == "?" or str(r.get("id", "")).startswith("NB-"):
        return "no bin"
    return f"C{r.get('cab')}-{r.get('bin')}"


def last4(pn):
    digits = "".join(ch for ch in str(pn) if ch.isdigit())
    return digits[-4:] if len(digits) >= 4 else str(pn)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pin", help="employee PIN; reads the live API instead of disk")
    ap.add_argument("--base", default="http://localhost:8080")
    args = ap.parse_args()

    if args.pin:
        bins, version, live = from_api(args.base, args.pin)
        source = f"live API {args.base}/api/bins"
    else:
        bins, version, live = from_disk()
        source = "local join of bins.json + additions.json + state.json"

    rows = sorted((r for r in bins if is_backstock(r)), key=sort_key)

    cols = ["PART NUMBER", "LAST4", "LOCATION", "BOXES", "FULL", "NOTE"]
    table = [[
        str(r.get("pn", "")),
        last4(r.get("pn", "")),
        location(r),
        str(int(r.get("boxes") or 0)),
        "Y" if r.get("binFull") else "N",
        str(r.get("boxNote") or ""),
    ] for r in rows]

    widths = [max(len(c), *(len(t[i]) for t in table)) if table else len(c)
              for i, c in enumerate(cols)]

    print(f"Bolt Cabinet — back stock")
    print(f"source : {source}")
    print(f"version: {version}")
    if not live:
        print("WARNING: state.json not found — box counts below are bins.json")
        print("         SEED values, not live counts.")
    print()
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)).rstrip())
    print("  ".join("-" * w for w in widths))
    for t in table:
        print("  ".join(v.ljust(w) for v, w in zip(t, widths)).rstrip())

    total_boxes = sum(int(r.get("boxes") or 0) for r in rows)
    print()
    print(f"{len(rows)} distinct back-stock parts, {total_boxes} boxes on hand")


if __name__ == "__main__":
    main()
