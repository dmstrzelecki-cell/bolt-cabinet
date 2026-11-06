"""Search: query Meilisearch, then filter every hit against the CURRENT config.

Default deny. A web hit is returned only if its URL is https:// and its exact
hostname is enabled in allowlist.yaml *right now*. A library hit only if the
SQLite row still exists and is enabled. A photo only if its opaque id still
exists and its album is still enabled in photo_albums.yaml. A stale index can
therefore never leak something a parent removed.
"""
from __future__ import annotations

import random
import re
import unicodedata
from typing import Any
from urllib.parse import unquote, urlsplit

from .config import ConfigStore, Host
from .db import DB, now_iso
from .meili import LIBRARY, MARK_POST, MARK_PRE, PHOTOS, WEB, Meili, in_filter

MAX_QUERY = 200
WEB_PER_PAGE = 10
LIB_PER_PAGE = 10
PHOTO_PER_PAGE = 24
MAX_PAGE = 50
SNIPPET_MAX = 320
TYPES = ("all", "web", "photos", "library")

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(s: Any, limit: int | None = None) -> str:
    """Strip control chars and our private highlight markers from crawled text."""
    s = _CTRL.sub("", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] if limit else s


def normalize_query(q: str) -> str:
    q = unicodedata.normalize("NFKC", q or "")
    return clean_text(q)


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def is_blocked(q: str, blocked: list[str]) -> bool:
    if not blocked:
        return False
    folded = _fold(q)
    tokens = re.findall(r"[a-z0-9]+", folded)
    padded = " " + " ".join(tokens) + " "
    compact = "".join(tokens)
    for term in blocked:
        t_tokens = re.findall(r"[a-z0-9]+", _fold(term))
        if not t_tokens:
            continue
        if f" {' '.join(t_tokens)} " in padded:
            return True
        if compact and compact == "".join(t_tokens):
            return True
    return False


def safe_https_url(url: Any, expected_host: str | None = None) -> str | None:
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        return None
    if p.port not in (None, 443):
        return None
    if expected_host is not None and p.hostname.lower() != expected_host:
        return None
    return url


def display_url(url: str) -> str:
    p = urlsplit(url)
    path = unquote(p.path or "/")
    s = (p.hostname or "").removeprefix("www.") + ("" if path == "/" else path.rstrip("/"))
    s = clean_text(s)
    return s if len(s) <= 70 else s[:67] + "…"


def _snippet(formatted: dict, hit: dict) -> str:
    body = clean_marked(formatted.get("body", ""))
    desc = clean_marked(formatted.get("description", ""))
    if MARK_PRE in desc:
        s = desc
    elif MARK_PRE in body:
        s = body
    else:
        s = desc or body
    return _trim_marked(s, SNIPPET_MAX)


def clean_marked(s: Any) -> str:
    """Like clean_text but keeps the highlight markers Meilisearch inserted."""
    s = str(s or "")
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _trim_marked(s: str, n: int) -> str:
    if len(s) <= n:
        return _balance(s)
    return _balance(s[:n].rstrip()) + "…"


def _balance(s: str) -> str:
    """Make markers strictly alternate PRE, POST, PRE, POST … and end closed."""
    out, open_ = [], False
    for ch in s:
        if ch == MARK_PRE:
            if open_:
                continue
            open_ = True
        elif ch == MARK_POST:
            if not open_:
                continue
            open_ = False
        out.append(ch)
    if open_:
        out.append(MARK_POST)
    return "".join(out)


class SearchService:
    def __init__(self, cfg: ConfigStore, meili: Meili, db: DB):
        self.cfg = cfg
        self.meili = meili
        self.db = db

    # -- scope ---------------------------------------------------------------
    def scope_hosts(self, tile: dict | None) -> frozenset[str]:
        """Approved hosts, narrowed by a tile's categories / hosts if given."""
        hosts: dict[str, Host] = self.cfg.hosts()
        approved = {h for h, v in hosts.items() if v.enabled}
        if not tile:
            return frozenset(approved)
        narrowed: set[str] = set()
        cats = set(tile.get("categories") or [])
        if cats:
            narrowed |= {h for h in approved if hosts[h].category in cats}
        if tile.get("hosts"):
            narrowed |= approved & set(tile["hosts"])
        if not cats and not tile.get("hosts"):
            return frozenset() if tile.get("include_library") else frozenset(approved)
        return frozenset(narrowed)

    def tile(self, tile_id: str | None) -> dict | None:
        if not tile_id:
            return None
        for t in self.cfg.tiles()["tiles"]:
            if t["id"] == tile_id and not t["hidden"]:
                return t
        return None

    # -- web -----------------------------------------------------------------
    def web(self, q: str, page: int, tile: dict | None = None) -> dict:
        allowed = self.scope_hosts(tile)
        if not allowed:
            return {"results": [], "page": page, "total_pages": 0}
        hosts = self.cfg.hosts()
        r = self.meili.search(WEB, {
            "q": q,
            "filter": in_filter("host", allowed),
            "page": page,
            "hitsPerPage": WEB_PER_PAGE,
            "attributesToRetrieve": ["id", "url", "host", "title", "description", "body"],
            "attributesToCrop": ["body", "description"],
            "cropLength": 32,
            "cropMarker": "…",
            "attributesToHighlight": ["title", "body", "description"],
            "highlightPreTag": MARK_PRE,
            "highlightPostTag": MARK_POST,
        })
        out = []
        for hit in r.get("hits", []):
            host = str(hit.get("host", "")).lower()
            if host not in allowed:
                continue
            url = safe_https_url(hit.get("url"), expected_host=host)
            if not url:
                continue
            fmt = hit.get("_formatted") or {}
            title = _trim_marked(clean_marked(fmt.get("title") or hit.get("title") or ""), 140) \
                or display_url(url)
            out.append({
                "kind": "web",
                "url": url,
                "title": title,
                "site": hosts[host].display_name,
                "initial": hosts[host].display_name[:1].upper(),
                "display_url": display_url(url),
                "snippet": _snippet(fmt, hit),
            })
        return {"results": out, "page": page,
                "total_pages": min(int(r.get("totalPages") or 0), MAX_PAGE)}

    # -- library -------------------------------------------------------------
    def library(self, q: str, page: int, per_page: int = LIB_PER_PAGE) -> dict:
        r = self.meili.search(LIBRARY, {
            "q": q, "page": page, "hitsPerPage": per_page,
            "attributesToRetrieve": ["id", "title", "note", "kind"],
            "attributesToCrop": ["note", "body"], "cropLength": 32, "cropMarker": "…",
            "attributesToHighlight": ["title", "note", "body"],
            "highlightPreTag": MARK_PRE, "highlightPostTag": MARK_POST,
        })
        ids = [str(h.get("id")) for h in r.get("hits", [])]
        rows = {}
        if ids:
            with self.db.conn() as c:
                qmarks = ",".join("?" * len(ids))
                for row in c.execute(f"SELECT * FROM library WHERE enabled=1 AND id IN ({qmarks})", ids):
                    rows[row["id"]] = row
        out = []
        for hit in r.get("hits", []):
            row = rows.get(str(hit.get("id")))
            if row is None:
                continue
            fmt = hit.get("_formatted") or {}
            item = {
                "kind": "library",
                "id": row["id"],
                "item_kind": row["kind"],
                "title": _trim_marked(clean_marked(fmt.get("title") or row["title"]), 140),
                "snippet": _trim_marked(clean_marked(fmt.get("note") or fmt.get("body") or row["note"]), SNIPPET_MAX),
            }
            if row["kind"] == "url":
                url = safe_https_url(row["url"])
                if not url:
                    continue
                item["url"] = url
                item["display_url"] = display_url(url)
            elif row["kind"] == "pdf":
                item["url"] = f"/library/{row['id']}"
                item["display_url"] = "Family library · PDF"
            else:
                item["url"] = f"/library/{row['id']}"
                item["display_url"] = "Family library · note"
            out.append(item)
        return {"results": out, "page": page,
                "total_pages": min(int(r.get("totalPages") or 0), MAX_PAGE)}

    # -- photos --------------------------------------------------------------
    def photos(self, q: str, page: int, per_page: int = PHOTO_PER_PAGE) -> dict:
        enabled = self.cfg.enabled_album_ids()
        if not enabled:
            return {"results": [], "page": page, "total_pages": 0}
        body: dict[str, Any] = {
            "q": q, "page": page, "hitsPerPage": per_page,
            "filter": in_filter("album_id", enabled),
            "attributesToRetrieve": ["id", "album_id", "album_name", "taken_date", "description"],
        }
        if not q:
            body["sort"] = ["taken_date:desc"]
        r = self.meili.search(PHOTOS, body)
        ids = [str(h.get("id")) for h in r.get("hits", [])]
        live: dict[str, Any] = {}
        if ids:
            with self.db.conn() as c:
                qmarks = ",".join("?" * len(ids))
                for row in c.execute(f"SELECT opaque_id, album_id FROM photos WHERE opaque_id IN ({qmarks})", ids):
                    live[row["opaque_id"]] = row
        out = []
        for hit in r.get("hits", []):
            row = live.get(str(hit.get("id")))
            if row is None or row["album_id"] not in enabled:
                continue
            out.append({
                "kind": "photo",
                "id": row["opaque_id"],
                "album": clean_text(hit.get("album_name"), 80),
                "date": clean_text(hit.get("taken_date"), 10),
                "description": clean_text(hit.get("description"), 200),
            })
        return {"results": out, "page": page,
                "total_pages": min(int(r.get("totalPages") or 0), MAX_PAGE)}

    # -- combined ------------------------------------------------------------
    def search(self, q: str, type_: str, page: int, tile_id: str | None) -> dict:
        tile = self.tile(tile_id)
        if tile_id and tile is None:
            return {"q": q, "type": type_, "blocked": False, "results": [], "page": page,
                    "total_pages": 0, "photos": [], "library": []}
        if tile and tile["action"] == "photos":
            type_ = "photos"
        if tile and tile["action"] == "search" and not q:
            q = tile["query"]
        resp: dict[str, Any] = {"q": q, "type": type_, "blocked": False, "page": page,
                                "tile": tile["label"] if tile else None}
        if is_blocked(q, self.cfg.filters()["blocked"]):
            resp.update(blocked=True, results=[], total_pages=0, photos=[], library=[])
            return resp
        self._remember(q, type_)
        lib_ok = tile is None or tile.get("include_library")
        if type_ == "web":
            resp.update(self.web(q, page, tile))
        elif type_ == "library":
            resp.update(self.library(q, page) if lib_ok or tile is None else
                        {"results": [], "page": page, "total_pages": 0})
        elif type_ == "photos":
            resp.update(self.photos(q, page))
        else:  # all: web + library first, then a compact photo strip
            library_only = bool(tile and tile.get("include_library")
                                and not tile.get("categories") and not tile.get("hosts"))
            if library_only:
                resp.update({"results": [], "page": page, "total_pages": 0})
            else:
                resp.update(self.web(q, page, tile))
            lib: list = []
            if page == 1 and lib_ok and (q or tile):
                lib = self.library(q, 1, per_page=10 if tile else 3)["results"]
            resp["library"] = lib
            resp["photos"] = (self.photos(q, 1, per_page=8)["results"]
                              if page == 1 and q and tile is None else [])
        resp.setdefault("photos", [])
        resp.setdefault("library", [])
        return resp

    def _remember(self, q: str, type_: str) -> None:
        if not q or not self.db.get_setting("store_history", False):
            return
        with self.db.conn() as c:
            c.execute("INSERT INTO search_history(ts, q, type) VALUES (?,?,?)", (now_iso(), q, type_))

    # -- extras ----------------------------------------------------------------
    def suggest(self, q: str) -> list[str]:
        q = normalize_query(q)
        if len(q) < 2 or is_blocked(q, self.cfg.filters()["blocked"]):
            return []
        out: list[str] = []
        ql = q.lower()
        for t in self.cfg.tiles()["tiles"]:
            if not t["hidden"] and t["label"].lower().startswith(ql):
                out.append(t["label"])
        allowed = self.scope_hosts(None)
        if allowed:
            r = self.meili.search(WEB, {
                "q": q, "limit": 8, "filter": in_filter("host", allowed),
                "attributesToRetrieve": ["title", "host"],
                "attributesToSearchOn": ["title"],
            })
            for h in r.get("hits", []):
                if str(h.get("host", "")).lower() not in allowed:
                    continue
                title = clean_text(h.get("title"), 80)
                if title and title not in out and not is_blocked(title, self.cfg.filters()["blocked"]):
                    out.append(title)
        return out[:6]

    def random_page(self, hosts: list[str] | None = None) -> str | None:
        pool = list(hosts or self.cfg.tiles().get("curious_hosts") or [])
        allowed = self.scope_hosts(None) & set(pool)
        if not allowed:
            return None
        flt = in_filter("host", allowed)
        r = self.meili.search(WEB, {"q": "", "filter": flt, "limit": 0})
        total = min(int(r.get("estimatedTotalHits") or 0), 1000)
        if total <= 0:
            return None
        for _ in range(3):
            r = self.meili.search(WEB, {"q": "", "filter": flt, "limit": 1,
                                        "offset": random.randrange(total),
                                        "attributesToRetrieve": ["url", "host"]})
            for h in r.get("hits", []):
                host = str(h.get("host", "")).lower()
                url = safe_https_url(h.get("url"), expected_host=host)
                if url and host in allowed:
                    return url
        return None
