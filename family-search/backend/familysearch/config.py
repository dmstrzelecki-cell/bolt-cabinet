"""Configuration: environment settings and the YAML/JSON files in config/.

The files in config/ are the source of truth for what is approved. They are
re-read on every request (cheap: mtime-cached), so a parent edit or a hand
edit takes effect on the very next request without a restart.

Both the backend and the crawler import this module. Keep it free of FastAPI.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PLACEHOLDER = "__SET_ME__"

# Exact lowercase DNS name. No scheme, port, path, wildcard, or IP literal.
HOST_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")
CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ALBUM_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

# filter/search = "broad" (results from many sites, shown in our search);
# open_url = "concise" (the tile opens one approved page directly).
# open_host is accepted for older files and stored as open_url.
TILE_ACTIONS = ("search", "filter", "open_url", "open_host", "photos", "random")
TILE_ICONS = (
    "book", "flask", "rocket", "paw", "column", "calculator", "letters",
    "news", "ballet", "lightbulb", "library", "camera", "globe", "pencil",
    "star", "music",
)

DEFAULT_PAGE_CAP = 2000
MAX_PAGE_CAP = 100_000
MAX_BOOST = 5


class ConfigError(ValueError):
    """A config file or a parent edit failed validation. Message is safe to show."""


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name, default)
    if v is not None:
        v = v.strip()
    return v


def is_set(value: str | None) -> bool:
    return bool(value) and value != PLACEHOLDER and PLACEHOLDER not in (value or "")


@dataclass(frozen=True)
class Settings:
    root: Path
    config_dir: Path
    data_dir: Path
    spool_dir: Path
    frontend_dir: Path
    public_origin: str
    cf_team_domain: str | None
    cf_aud: str | None
    parent_emails: frozenset[str]
    allowed_emails: frozenset[str]
    meili_url: str
    meili_key: str | None
    csrf_secret: str | None
    immich_url: str | None
    immich_api_key: str | None
    search_rate_per_min: int
    thumb_rate_per_min: int
    notify_url: str | None = None
    notify_format: str = "ntfy"

    @property
    def auth_configured(self) -> bool:
        return (
            is_set(self.cf_team_domain)
            and is_set(self.cf_aud)
            and bool(self.parent_emails)
            and is_set(self.csrf_secret)
        )

    @property
    def immich_configured(self) -> bool:
        return is_set(self.immich_url) and is_set(self.immich_api_key)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app" / "app.db"

    @property
    def library_dir(self) -> Path:
        return self.data_dir / "app" / "library"

    @property
    def crawl_status_path(self) -> Path:
        return self.spool_dir / "crawl-status.json"


def _emails(raw: str | None) -> frozenset[str]:
    if not is_set(raw):
        return frozenset()
    return frozenset(e.strip().lower() for e in raw.split(",") if "@" in e)


def load_settings() -> Settings:
    root = Path(env("FS_ROOT", "/opt/family-search"))
    data = Path(env("FS_DATA_DIR", "/var/lib/family-search"))
    team = env("CF_ACCESS_TEAM_DOMAIN")
    if team:
        team = team.removeprefix("https://").rstrip("/").lower()
    return Settings(
        root=root,
        config_dir=Path(env("FS_CONFIG_DIR", str(root / "config"))),
        data_dir=data,
        spool_dir=Path(env("FS_SPOOL_DIR", str(data / "spool"))),
        frontend_dir=Path(env("FS_FRONTEND_DIR", str(root / "frontend"))),
        public_origin=(env("FS_PUBLIC_ORIGIN", "https://search.thestrzs.net") or "").rstrip("/"),
        cf_team_domain=team,
        cf_aud=env("CF_ACCESS_AUD"),
        parent_emails=_emails(env("PARENT_EMAILS")),
        allowed_emails=_emails(env("ALLOWED_EMAILS")),
        meili_url=(env("MEILI_URL", "http://127.0.0.1:7700") or "").rstrip("/"),
        meili_key=env("MEILI_MASTER_KEY"),
        csrf_secret=env("CSRF_SECRET"),
        immich_url=(env("IMMICH_URL") or "").rstrip("/") or None,
        immich_api_key=env("IMMICH_API_KEY"),
        search_rate_per_min=int(env("FS_SEARCH_RATE", "60") or 60),
        thumb_rate_per_min=int(env("FS_THUMB_RATE", "300") or 300),
        notify_url=env("NOTIFY_URL") if is_set(env("NOTIFY_URL")) else None,
        notify_format=(env("NOTIFY_FORMAT", "ntfy") or "ntfy").lower(),
    )


# --------------------------------------------------------------------------
# File helpers
# --------------------------------------------------------------------------

def atomic_write(path: Path, data: str | bytes, mode: int = 0o640) -> None:
    """temp file + fsync + os.replace, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data.encode() if isinstance(data, str) else data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def backup_config_file(path: Path, keep: int = 30) -> None:
    """Dated copy of a config file into config/.history before it changes."""
    if not path.exists():
        return
    hist = path.parent / ".history"
    hist.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    dest = hist / f"{path.stem}.{stamp}{path.suffix}"
    n = 1
    while dest.exists():
        dest = hist / f"{path.stem}.{stamp}-{n}{path.suffix}"
        n += 1
    shutil.copy2(path, dest)
    old = sorted(hist.glob(f"{path.stem}.*{path.suffix}"))
    for f in old[:-keep]:
        f.unlink(missing_ok=True)


class _Cached:
    """mtime-cached loader for one config file."""

    def __init__(self, path_fn, parse_fn):
        self._path_fn = path_fn
        self._parse = parse_fn
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._value: Any = None

    def get(self):
        path: Path = self._path_fn()
        try:
            st = path.stat()
            key = (str(path), st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            key = (str(path), None, None)
        with self._lock:
            if key != self._key:
                raw = path.read_text() if key[1] is not None else ""
                self._value = self._parse(raw)
                self._key = key
            return self._value

    def invalidate(self):
        with self._lock:
            self._key = None


# --------------------------------------------------------------------------
# allowlist.yaml
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Host:
    host: str
    category: str
    crawl: bool = True
    enabled: bool = True
    page_cap: int = DEFAULT_PAGE_CAP
    boost: int = 0
    name: str = ""
    schedule_day: int | None = None  # 0=Mon … 6=Sun; None = spread by hash
    source: str = "crawl"            # "crawl" | "wikimedia-dump"

    @property
    def display_name(self) -> str:
        return self.name or self.host.removeprefix("www.")

    def to_yaml(self) -> dict:
        d: dict[str, Any] = {"category": self.category}
        if not self.crawl:
            d["crawl"] = False
        if not self.enabled:
            d["enabled"] = False
        if self.page_cap != DEFAULT_PAGE_CAP:
            d["page_cap"] = self.page_cap
        if self.boost:
            d["boost"] = self.boost
        if self.name:
            d["name"] = self.name
        if self.schedule_day is not None:
            d["schedule_day"] = self.schedule_day
        if self.source != "crawl":
            d["source"] = self.source
        return d


def validate_host(host: Any) -> str:
    if not isinstance(host, str):
        raise ConfigError("host must be text")
    h = host.strip().lower().rstrip(".")
    if not HOST_RE.match(h):
        raise ConfigError(f"not an exact hostname: {host!r}")
    try:
        ipaddress.ip_address(h)
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip or re.fullmatch(r"[0-9.]+", h):
        raise ConfigError("IP addresses are not allowed; use a hostname")
    return h


def _host_from(host: str, raw: Any) -> Host:
    h = validate_host(host)
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{h}: settings must be a mapping")
    cat = str(raw.get("category", "general")).strip().lower()
    if not CATEGORY_RE.match(cat):
        raise ConfigError(f"{h}: bad category {cat!r}")
    try:
        cap = int(raw.get("page_cap", DEFAULT_PAGE_CAP))
        boost = int(raw.get("boost", 0))
    except (TypeError, ValueError):
        raise ConfigError(f"{h}: page_cap and boost must be whole numbers")
    if not 1 <= cap <= MAX_PAGE_CAP:
        raise ConfigError(f"{h}: page_cap must be 1–{MAX_PAGE_CAP}")
    if not 0 <= boost <= MAX_BOOST:
        raise ConfigError(f"{h}: boost must be 0–{MAX_BOOST}")
    day = raw.get("schedule_day")
    if day is not None:
        try:
            day = int(day)
        except (TypeError, ValueError):
            raise ConfigError(f"{h}: schedule_day must be 0–6")
        if not 0 <= day <= 6:
            raise ConfigError(f"{h}: schedule_day must be 0–6")
    source = str(raw.get("source", "crawl"))
    if source not in ("crawl", "wikimedia-dump"):
        raise ConfigError(f"{h}: unknown source {source!r}")
    name = str(raw.get("name", "")).strip()[:60]
    return Host(
        host=h, category=cat, crawl=bool(raw.get("crawl", True)),
        enabled=bool(raw.get("enabled", True)), page_cap=cap, boost=boost,
        name=name, schedule_day=day, source=source,
    )


def parse_allowlist(text: str) -> dict[str, Host]:
    doc = yaml.safe_load(text) or {}
    if not isinstance(doc, dict):
        raise ConfigError("allowlist.yaml must be a mapping")
    hosts = doc.get("hosts") or {}
    if not isinstance(hosts, dict):
        raise ConfigError("allowlist.yaml: hosts must be a mapping")
    out: dict[str, Host] = {}
    for k, v in hosts.items():
        h = _host_from(k, v)
        out[h.host] = h
    return out


def dump_allowlist(hosts: dict[str, Host]) -> str:
    body = {"hosts": {h.host: h.to_yaml() for h in sorted(hosts.values(), key=lambda x: x.host)}}
    head = (
        "# Approved hosts. Exact hostnames only: a parent domain does NOT approve\n"
        "# its subdomains. Edited by the /parent page; a dated copy of every\n"
        "# previous version is kept in config/.history/.\n"
    )
    return head + yaml.safe_dump(body, sort_keys=False, default_flow_style=None, width=100)


# --------------------------------------------------------------------------
# tiles.yaml
# --------------------------------------------------------------------------

def _str_list(v: Any, what: str, pattern: re.Pattern | None = None) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list):
        raise ConfigError(f"{what} must be a list")
    out = []
    for item in v:
        s = str(item).strip().lower()
        if pattern and not pattern.match(s):
            raise ConfigError(f"{what}: bad value {item!r}")
        out.append(s)
    return out


MAX_PREFIX_DEPTH = 6


def url_prefixes(url: str) -> list[str]:
    """Every section a page sits in, as 'host/seg1/seg2/' keys, from the site
    root down to the page itself. Indexed with each page so a tile can be
    narrowed to part of a site (e.g. kids.nationalgeographic.com/animals/)."""
    from urllib.parse import unquote, urlsplit
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if not host:
        return []
    segs = [unquote(x).lower() for x in (p.path or "/").split("/") if x][:MAX_PREFIX_DEPTH]
    return [host + "/" + "".join(f"{x}/" for x in segs[:i]) for i in range(len(segs) + 1)]


def section_key(raw: Any) -> str:
    """Parent-entered section link -> 'host/path/' key. Accepts
    'https://host/path', 'host/path' or 'https://host/path/'."""
    from urllib.parse import urlsplit
    text = str(raw or "").strip()
    if not text:
        raise ConfigError("empty section link")
    if "://" not in text:
        text = "https://" + text
    p = urlsplit(text)
    if p.scheme != "https":
        raise ConfigError(f"section links must be https:// ({raw!r})")
    if p.query or p.fragment:
        raise ConfigError(f"section links can't have ?query or #fragment ({raw!r})")
    host = validate_host(p.hostname or "")
    keys = url_prefixes(f"https://{host}{p.path or '/'}")
    return keys[-1]


def section_url(key: str) -> str:
    return "https://" + key


def validate_page_url(raw: Any) -> str:
    """A single https:// page a 'concise' tile opens. Approval of its host is
    checked at serve time, so removing the site also removes the tile."""
    from urllib.parse import urlsplit, urlunsplit
    text = str(raw or "").strip()
    if "://" not in text and text:
        text = "https://" + text
    try:
        p = urlsplit(text)
        port = p.port
    except ValueError:
        raise ConfigError(f"not a valid link: {raw!r}")
    if p.scheme != "https":
        raise ConfigError(f"tile links must start with https:// ({raw!r})")
    if p.username or p.password or port not in (None, 443) or len(text) > 2048:
        raise ConfigError(f"not an allowed link: {raw!r}")
    host = validate_host(p.hostname or "")
    return urlunsplit(("https", host, p.path or "/", p.query, ""))


def validate_tile(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError("each tile must be a mapping")
    tid = str(raw.get("id", "")).strip().lower()
    if not TILE_ID_RE.match(tid):
        raise ConfigError(f"tile id {tid!r} must be lowercase letters, digits, dashes")
    label = str(raw.get("label", "")).strip()
    if not 1 <= len(label) <= 24:
        raise ConfigError(f"tile {tid}: label must be 1–24 characters")
    action = str(raw.get("action", ""))
    if action not in TILE_ACTIONS:
        raise ConfigError(f"tile {tid}: action must be one of {', '.join(TILE_ACTIONS)}")
    icon = str(raw.get("icon", "star"))
    if icon not in TILE_ICONS:
        icon = "star"
    t = {
        "id": tid, "label": label, "action": action, "icon": icon,
        "hidden": bool(raw.get("hidden", False)),
        "categories": _str_list(raw.get("categories"), f"tile {tid} categories", CATEGORY_RE),
        "hosts": [validate_host(h) for h in (raw.get("hosts") or [])],
        "sections": list(dict.fromkeys(section_key(x) for x in (raw.get("sections") or []))),
        "query": str(raw.get("query", "")).strip()[:100],
        "include_library": bool(raw.get("include_library", False)),
        "url": validate_page_url(raw["url"]) if raw.get("url") else "",
    }
    if action == "open_host":  # legacy: open a site's home page
        if not t["hosts"]:
            raise ConfigError(f"tile {tid}: needs at least one host")
        t["action"], t["url"] = "open_url", f"https://{t['hosts'][0]}/"
    if t["action"] == "open_url" and not t["url"]:
        raise ConfigError(f"tile {tid}: a concise tile needs the one link it opens")
    if action == "search" and not t["query"]:
        raise ConfigError(f"tile {tid}: a search tile needs a query")
    if raw.get("sections") is not None and not isinstance(raw.get("sections"), list):
        raise ConfigError(f"tile {tid}: sections must be a list of links")
    if len(t["sections"]) > 40:
        raise ConfigError(f"tile {tid}: 40 sections at most")
    if action == "random" and not t["hosts"]:
        raise ConfigError(f"tile {tid}: needs at least one host")
    if action == "filter" and not (t["categories"] or t["hosts"] or t["sections"]
                                   or t["include_library"]):
        raise ConfigError(f"tile {tid}: a filter tile needs categories, sites or sections")
    return t


def parse_tiles(text: str) -> dict:
    doc = yaml.safe_load(text) or {}
    if not isinstance(doc, dict):
        raise ConfigError("tiles.yaml must be a mapping")
    tiles = [validate_tile(t) for t in (doc.get("tiles") or [])]
    ids = [t["id"] for t in tiles]
    if len(ids) != len(set(ids)):
        raise ConfigError("tile ids must be unique")
    curious = [validate_host(h) for h in (doc.get("curious_hosts") or [])]
    return {"tiles": tiles, "curious_hosts": curious}


def dump_tiles(doc: dict) -> str:
    tiles = []
    for t in doc["tiles"]:
        d = {"id": t["id"], "label": t["label"], "icon": t["icon"], "action": t["action"]}
        for k in ("categories", "hosts"):
            if t.get(k):
                d[k] = t[k]
        if t.get("sections"):
            d["sections"] = [section_url(k) for k in t["sections"]]
        if t.get("query"):
            d["query"] = t["query"]
        if t.get("include_library"):
            d["include_library"] = True
        if t.get("url"):
            d["url"] = t["url"]
        if t.get("hidden"):
            d["hidden"] = True
        tiles.append(d)
    head = "# Home-page tiles, in display order. Edited by the /parent page.\n"
    return head + yaml.safe_dump(
        {"curious_hosts": doc.get("curious_hosts", []), "tiles": tiles},
        sort_keys=False, default_flow_style=None, width=100,
    )


# --------------------------------------------------------------------------
# photo_albums.yaml
# --------------------------------------------------------------------------

def parse_albums(text: str) -> list[dict]:
    doc = yaml.safe_load(text) or {}
    if not isinstance(doc, dict):
        raise ConfigError("photo_albums.yaml must be a mapping")
    out, seen = [], set()
    for a in doc.get("albums") or []:
        if not isinstance(a, dict):
            raise ConfigError("each album must be a mapping")
        aid = str(a.get("id", "")).strip().lower()
        if not ALBUM_ID_RE.match(aid):
            raise ConfigError(f"album id {aid!r} is not an Immich album UUID")
        if aid in seen:
            continue
        seen.add(aid)
        out.append({
            "id": aid,
            "name": str(a.get("name", "")).strip()[:80],
            "enabled": bool(a.get("enabled", True)),
        })
    return out


def dump_albums(albums: list[dict]) -> str:
    head = (
        "# Immich albums the family-search user may show. Removing an album here\n"
        "# revokes its photos immediately. Edited by the /parent page.\n"
    )
    rows = []
    for a in albums:
        r = {"id": a["id"], "name": a.get("name", "")}
        if not a.get("enabled", True):
            r["enabled"] = False
        rows.append(r)
    return head + yaml.safe_dump({"albums": rows}, sort_keys=False)


# --------------------------------------------------------------------------
# blocked_terms.yaml (blocked terms + synonyms)
# --------------------------------------------------------------------------

def _norm_term(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())[:60]


def parse_filters(text: str) -> dict:
    doc = yaml.safe_load(text) or {}
    if not isinstance(doc, dict):
        raise ConfigError("blocked_terms.yaml must be a mapping")
    blocked = sorted({_norm_term(t) for t in (doc.get("blocked") or []) if _norm_term(t)})
    watch = sorted({_norm_term(t) for t in (doc.get("watch") or []) if _norm_term(t)} - set(blocked))
    syn_raw = doc.get("synonyms") or {}
    if not isinstance(syn_raw, dict):
        raise ConfigError("synonyms must be a mapping of word -> list of words")
    synonyms: dict[str, list[str]] = {}
    for k, v in syn_raw.items():
        key = _norm_term(k)
        vals = v if isinstance(v, list) else [v]
        vals = [_norm_term(x) for x in vals if _norm_term(x) and _norm_term(x) != key]
        if key and vals:
            synonyms[key] = sorted(set(vals))
    return {"blocked": blocked, "watch": watch, "synonyms": synonyms}


def dump_filters(doc: dict) -> str:
    head = (
        "# blocked: a search containing any of these words or phrases shows a calm\n"
        "#          'ask a grown-up' message instead of results. Flagged in the\n"
        "#          activity log (and notified) when monitoring is on.\n"
        "# watch:   searches are allowed as normal, but flagged for a parent to see\n"
        "#          (and notified) when monitoring is on — e.g. names, apps, self-harm words.\n"
        "# synonyms: word -> words that should also match.\n"
    )
    return head + yaml.safe_dump(
        {"blocked": doc["blocked"], "watch": doc.get("watch", []), "synonyms": doc["synonyms"]},
        sort_keys=False, allow_unicode=True,
    )


# --------------------------------------------------------------------------
# theme.json
# --------------------------------------------------------------------------

COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
THEME_TOKENS = ("bg", "surface", "text", "muted", "accent", "accent-text",
                "secondary", "link", "border", "focus", "warn-bg", "warn-text")


def parse_theme(text: str) -> dict:
    doc = json.loads(text or "{}")
    out = {
        "site_name": str(doc.get("site_name", "Family Search"))[:40],
        "tagline": str(doc.get("tagline", ""))[:80],
        "wordmark": [str(x)[:20] for x in (doc.get("wordmark") or ["family", "search"])][:2],
        "light": {}, "dark": {},
    }
    for mode in ("light", "dark"):
        src = doc.get(mode) or {}
        for k in THEME_TOKENS:
            v = src.get(k)
            if isinstance(v, str) and COLOR_RE.match(v):
                out[mode][k] = v
    return out


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

@dataclass
class ConfigStore:
    settings: Settings
    _allow: _Cached = field(init=False)
    _tiles: _Cached = field(init=False)
    _albums: _Cached = field(init=False)
    _filters: _Cached = field(init=False)
    _theme: _Cached = field(init=False)
    write_lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        d = lambda name: (lambda: self.settings.config_dir / name)  # noqa: E731
        self._allow = _Cached(d("allowlist.yaml"), parse_allowlist)
        self._tiles = _Cached(d("tiles.yaml"), parse_tiles)
        self._albums = _Cached(d("photo_albums.yaml"), parse_albums)
        self._filters = _Cached(d("blocked_terms.yaml"), parse_filters)
        self._theme = _Cached(d("theme.json"), parse_theme)

    def path(self, name: str) -> Path:
        return self.settings.config_dir / name

    # readers -------------------------------------------------------------
    def hosts(self) -> dict[str, Host]:
        return self._allow.get()

    def approved_hosts(self) -> frozenset[str]:
        """Hosts whose results may be shown right now."""
        return frozenset(h for h, v in self.hosts().items() if v.enabled)

    def tiles(self) -> dict:
        return self._tiles.get()

    def albums(self) -> list[dict]:
        return self._albums.get()

    def enabled_album_ids(self) -> frozenset[str]:
        return frozenset(a["id"] for a in self.albums() if a["enabled"])

    def filters(self) -> dict:
        return self._filters.get()

    def theme(self) -> dict:
        return self._theme.get()

    # writers (callers hold write_lock) -----------------------------------
    def _write(self, name: str, text: str, cache: _Cached, parse) -> None:
        parse(text)  # never write something we could not read back
        p = self.path(name)
        backup_config_file(p)
        atomic_write(p, text)
        cache.invalidate()

    def save_hosts(self, hosts: dict[str, Host]) -> None:
        self._write("allowlist.yaml", dump_allowlist(hosts), self._allow, parse_allowlist)

    def save_tiles(self, doc: dict) -> None:
        self._write("tiles.yaml", dump_tiles(doc), self._tiles, parse_tiles)

    def save_albums(self, albums: list[dict]) -> None:
        self._write("photo_albums.yaml", dump_albums(albums), self._albums, parse_albums)

    def save_filters(self, doc: dict) -> None:
        self._write("blocked_terms.yaml", dump_filters(doc), self._filters, parse_filters)
