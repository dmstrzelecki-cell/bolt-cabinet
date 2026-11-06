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

from .config import ConfigStore, Host, url_prefixes
from .activity import ActivityService, match_term
from .db import DB
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
    return match_term(q, blocked) is not None


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


class Scope:
    def __init__(self, approved: frozenset[str], hosts: frozenset[str],
                 sections: frozenset[str], narrowed: bool):
        self.approved = approved
        self.hosts = hosts & approved
        self.sections = sections
        self.narrowed = narrowed

    @property
    def empty(self) -> bool:
        return not self.hosts and not self.sections

    def meili_filter(self) -> Any:
        base = in_filter("host", self.approved)
        if not self.narrowed:
            return base
        either = []
        if self.hosts:
            either.append(in_filter("host", self.hosts))
        if self.sections:
            either.append(in_filter("prefixes", self.sections))
        return [base, either]  # base AND (whole sites OR sections)

    def allows(self, url: str, host: str) -> bool:
        if host not in self.approved:
            return False
        if host in self.hosts:
            return True
        return bool(self.sections) and any(p in self.sections for p in url_prefixes(url))


class SearchService:
    def __init__(self, cfg: ConfigStore, meili: Meili, db: DB,
                 activity: "ActivityService | None" = None):
        self.cfg = cfg
        self.meili = meili
        self.db = db
        self.activity = activity

    # -- scope ---------------------------------------------------------------
    def scope(self, tile: dict | None) -> "Scope":
        """What a search may return right now: approved hosts, optionally
        narrowed by a tile to whole sites (categories / hosts) OR sections
        (parts of a site). Sections on hosts that aren't approved are ignored."""
        hosts: dict[str, Host] = self.cfg.hosts()
        approved = frozenset(h for h, v in hosts.items() if v.enabled)
        if not tile:
            return Scope(approved, approved, frozenset(), narrowed=False)
        whole: set[str] = set()
        cats = set(tile.get("categories") or [])
        if cats:
            whole |= {h for h in approved if hosts[h].category in cats}
        whole |= approved & set(tile.get("hosts") or [])
        sections = frozenset(k for k in tile.get("sections") or [] if k.split("/", 1)[0] in approved)
        if not (cats or tile.get("hosts") or tile.get("sections")):
            if tile.get("include_library"):
                return Scope(approved, frozenset(), frozenset(), narrowed=True)
            return Scope(approved, approved, frozenset(), narrowed=False)
        return Scope(approved, frozenset(whole), sections, narrowed=True)

    def scope_hosts(self, tile: dict | None) -> frozenset[str]:
        return self.scope(tile).hosts

    def tile(self, tile_id: str | None) -> dict | None:
        if not tile_id:
            return None
        for t in self.cfg.tiles()["tiles"]:
            if t["id"] == tile_id and not t["hidden"]:
                return t
        return None

    # -- web -----------------------------------------------------------------
    def web(self, q: str, page: int, tile: dict | None = None) -> dict:
        sc = self.scope(tile)
        if sc.empty:
            return {"results": [], "page": page, "total_pages": 0}
        hosts = self.cfg.hosts()
        r = self.meili.search(WEB, {
            "q": q,
            "filter": sc.meili_filter(),
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
            url = safe_https_url(hit.get("url"), expected_host=host)
            if not url or not sc.allows(url, host):
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
        filters = self.cfg.filters()
        blocked_term = match_term(q, filters["blocked"])
        if blocked_term:
            resp.update(blocked=True, results=[], total_pages=0, photos=[], library=[], suggest_ask=False)
            self._log(q, type_, tile, page, 0, "blocked", blocked_term)
            return resp
        watch_term = match_term(q, filters.get("watch") or [])
        lib_ok = tile is None or tile.get("include_library")
        if type_ == "web":
            resp.update(self.web(q, page, tile))
        elif type_ == "library":
            resp.update(self.library(q, page) if lib_ok or tile is None else
                        {"results": [], "page": page, "total_pages": 0})
        elif type_ == "photos":
            resp.update(self.photos(q, page))
        else:  # all: web + library first, then a compact photo strip
            library_only = bool(tile and tile.get("include_library") and not tile.get("categories")
                                and not tile.get("hosts") and not tile.get("sections"))
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
        # A watch word still gets results, plus a gentle "ask a grown-up" card.
        # The response never says which word matched.
        resp["suggest_ask"] = bool(watch_term) and bool(self.db.get_setting("watch_prompt", True))
        count = len(resp.get("results") or []) + len(resp["library"]) + len(resp["photos"])
        self._log(q, type_, tile, page, count, "watch" if watch_term else "", watch_term or "")
        return resp

    def _log(self, q: str, type_: str, tile: dict | None, page: int, count: int,
             flag: str, term: str) -> None:
        # Only the first page of a search is "activity"; paging is not a new search.
        if self.activity is None or (page != 1 and not flag):
            return
        if not q and tile is None and type_ != "photos":
            return
        kind = "tile" if tile and not q else "search"
        self.activity.record(kind, q=q, tab=type_, tile=tile["label"] if tile else "",
                             results=count, flag=flag, term=term)

    def tile_count(self, tile: dict) -> int | None:
        """Roughly how many approved pages a tile reaches (parent page hint)."""
        if tile["action"] not in ("filter", "search"):
            return None
        sc = self.scope(tile)
        if sc.empty:
            return 0
        r = self.meili.search(WEB, {"q": tile.get("query") if tile["action"] == "search" else "",
                                    "filter": sc.meili_filter(), "limit": 0})
        return int(r.get("estimatedTotalHits") or 0)

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
