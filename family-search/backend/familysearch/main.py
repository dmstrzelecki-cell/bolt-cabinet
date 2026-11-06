"""Family Search backend: the only thing the browser ever talks to.

Every request — pages, static files, API — must carry a valid Cloudflare
Access JWT. Parent pages and APIs additionally require an email in
PARENT_EMAILS. Every state-changing request needs a same-origin Origin and a
CSRF token bound to the signed-in email.

Run: uvicorn --factory familysearch.main:create_app --app-dir backend --port 8080
"""
from __future__ import annotations

import contextlib
import json
import mimetypes
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .auth import (JWT_HEADER, AccessVerifier, AuthError, Identity, RateLimiter, csrf_ok,
                   csrf_token, identify)
from .config import ConfigError, ConfigStore, Settings, load_settings
from .db import DB, now_iso
from .library import MAX_PDF, LibraryService
from .meili import Meili, MeiliError
from .parent import ParentService
from .photos import Immich, ImmichError, PhotoService
from .search import MAX_QUERY, TYPES, SearchService, normalize_query

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; manifest-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-src 'none'; frame-ancestors 'none'; "
       "upgrade-insecure-requests")
PDF_CSP = "sandbox; default-src 'none'; frame-ancestors 'none'; object-src 'none'"
PERMISSIONS_POLICY = ("accelerometer=(), autoplay=(), camera=(), clipboard-read=(), "
                      "display-capture=(), encrypted-media=(), fullscreen=(self), "
                      "geolocation=(), gyroscope=(), hid=(), magnetometer=(), microphone=(), "
                      "midi=(), payment=(), picture-in-picture=(), publickey-credentials-get=(), "
                      "screen-wake-lock=(), serial=(), usb=(), xr-spatial-tracking=(), "
                      "interest-cohort=(), browsing-topics=()")
NO_STORE = "private, no-store"
SAFE_METHODS = ("GET", "HEAD")

STATIC_TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
                ".png": "image/png", ".webmanifest": "application/manifest+json",
                ".ico": "image/x-icon", ".woff2": "font/woff2", ".html": "text/html"}


def _client_ip(request: Request) -> str:
    # Logging only — never used for identity or authorization.
    return (request.headers.get("cf-connecting-ip")
            or (request.client.host if request.client else "?"))[:64]


def _err(status: int, msg: str, **extra) -> JSONResponse:
    return JSONResponse({"error": msg, **extra}, status_code=status,
                        headers={"Cache-Control": NO_STORE})


class App:
    """Holds services; built once per process (or per test)."""

    def __init__(self, settings: Settings, verifier: AccessVerifier | None = None,
                 immich: Immich | None = None, meili: Meili | None = None):
        self.s = settings
        self.cfg = ConfigStore(settings)
        self.db = DB(settings.db_path)
        self.meili = meili or Meili(settings.meili_url, settings.meili_key)
        if verifier is None and settings.auth_configured:
            verifier = AccessVerifier(settings.cf_team_domain, settings.cf_aud)
        self.verifier = verifier
        if immich is None and settings.immich_configured:
            immich = Immich(settings.immich_url, settings.immich_api_key)
        self.photos = PhotoService(self.cfg, self.db, self.meili, immich)
        self.search = SearchService(self.cfg, self.meili, self.db)
        self.library = LibraryService(self.db, self.meili, settings.library_dir)
        self.parent = ParentService(self.cfg, self.db, self.meili, self.photos)
        self.limiter = RateLimiter()
        self._denied_lock = threading.Lock()
        self._denied_seen: dict[tuple[str, str], float] = {}
        self._sync_lock = threading.Lock()

    def note_denied(self, reason: str, request: Request, email: str = "") -> None:
        """Audit denied access, at most once a minute per (reason, source)."""
        ip = _client_ip(request)
        key = (reason, ip)
        now = time.monotonic()
        with self._denied_lock:
            if now - self._denied_seen.get(key, -1e9) < 60:
                return
            self._denied_seen[key] = now
            if len(self._denied_seen) > 5000:
                self._denied_seen.clear()
        try:
            self.db.audit(email or "-", "denied", reason,
                          {"path": request.url.path[:200], "ip": ip})
        except Exception:
            pass


def create_app(settings: Settings | None = None, verifier: AccessVerifier | None = None,
               immich: Immich | None = None, meili: Meili | None = None) -> FastAPI:
    settings = settings or load_settings()
    core = App(settings, verifier, immich, meili)
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        try:
            core.meili.ensure_indexes(core.cfg.filters()["synonyms"])
        except Exception:
            pass  # search answers 503 until Meilisearch is up; nothing else depends on it
        try:
            core.photos.revoke_disabled()
        except Exception:
            pass
        yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.core = core
    fe: Path = settings.frontend_dir
    public_host = settings.public_origin.split("://", 1)[-1]

    # ---------------------------------------------------------------- middleware
    class Gate(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            resp = await self._dispatch(request, call_next)
            h = resp.headers
            h["Content-Security-Policy"] = h.get("Content-Security-Policy", CSP)
            h["X-Content-Type-Options"] = "nosniff"
            h["Referrer-Policy"] = "no-referrer"
            h["X-Frame-Options"] = "DENY"
            h["Permissions-Policy"] = PERMISSIONS_POLICY
            h["Cross-Origin-Opener-Policy"] = "same-origin"
            h["Cross-Origin-Resource-Policy"] = "same-origin"
            if "cache-control" not in h:
                h["Cache-Control"] = NO_STORE
            return resp

        async def _dispatch(self, request: Request, call_next):
            path = request.url.path
            if request.method not in ("GET", "HEAD", "POST"):
                return _err(405, "method not allowed")
            if core.verifier is None:
                core.note_denied("app not configured", request)
                return _err(503, "Family Search is not configured yet.")
            token = request.headers.get(JWT_HEADER, "")
            try:
                claims = core.verifier.verify(token)
                ident = identify(claims, settings.parent_emails, settings.allowed_emails)
            except AuthError as e:
                core.note_denied(e.reason, request)
                return _err(e.status if e.status in (401, 403, 503) else 401,
                            "Please sign in again.", signin=True)
            request.state.ident = ident

            if path == "/parent" or path.startswith("/api/parent/") or path.startswith("/static/parent"):
                if not ident.is_parent:
                    core.note_denied("child tried parent area", request, ident.email)
                    return _err(403, "This page is for grown-ups.")

            if request.method not in SAFE_METHODS:
                origin = request.headers.get("origin")
                site = request.headers.get("sec-fetch-site")
                if origin is not None and origin.rstrip("/") != settings.public_origin:
                    core.note_denied("cross-origin write", request, ident.email)
                    return _err(403, "cross-origin request refused")
                if origin is None and site not in ("same-origin",):
                    core.note_denied("write without origin", request, ident.email)
                    return _err(403, "cross-origin request refused")
                if not csrf_ok(settings.csrf_secret, ident.email, request.headers.get("x-csrf-token")):
                    core.note_denied("bad csrf token", request, ident.email)
                    return _err(403, "Please reload the page and try again.")
            try:
                return await call_next(request)
            except MeiliError:
                return _err(503, "Search is taking a nap. Try again in a minute.", unavailable=True)

    app.add_middleware(Gate)

    def ident(request: Request) -> Identity:
        return request.state.ident

    async def json_body(request: Request, limit: int = 64 * 1024) -> dict:
        raw = await request.body()
        if len(raw) > limit:
            raise ConfigError("That is too much text.")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise ConfigError("Bad request.")
        if not isinstance(body, dict):
            raise ConfigError("Bad request.")
        return body

    @app.exception_handler(ConfigError)
    async def _config_error(request: Request, exc: ConfigError):
        return _err(400, str(exc))

    # ---------------------------------------------------------------- pages
    def page(name: str) -> Response:
        return FileResponse(fe / name, media_type="text/html",
                            headers={"Cache-Control": NO_STORE})

    @app.get("/")
    def home():
        return page("index.html")

    @app.get("/parent")
    def parent_page():
        return page("parent.html")

    @app.get("/library/{lid}")
    def library_page(lid: str):
        return page("index.html")  # the SPA shows the note / PDF link

    @app.get("/manifest.webmanifest")
    def manifest():
        t = core.cfg.theme()
        body = {
            "name": t["site_name"], "short_name": t["site_name"][:12],
            "start_url": "/", "scope": "/", "display": "standalone",
            "background_color": t["light"].get("bg", "#fbf7f2"),
            "theme_color": t["light"].get("bg", "#fbf7f2"),
            "icons": [
                {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512",
                 "type": "image/png", "purpose": "maskable"},
            ],
        }
        return Response(json.dumps(body), media_type="application/manifest+json",
                        headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/theme.css")
    def theme_css():
        t = core.cfg.theme()

        def block(tokens: dict) -> str:
            return "".join(f"--{k}:{v};" for k, v in tokens.items())
        css = (f":root{{{block(t['light'])}}}\n"
               f"@media (prefers-color-scheme: dark){{:root{{{block(t['dark'])}}}}}\n")
        return Response(css, media_type="text/css", headers={"Cache-Control": "private, max-age=300"})

    @app.get("/static/{path:path}")
    def static(path: str):
        base = fe.resolve()
        p = (base / path).resolve()
        if base not in p.parents or not p.is_file() or p.suffix not in STATIC_TYPES \
                or p.name.endswith(".html"):
            return _err(404, "not found")
        return FileResponse(p, media_type=STATIC_TYPES[p.suffix],
                            headers={"Cache-Control": "private, max-age=300"})

    @app.get("/favicon.ico")
    def favicon():
        return FileResponse(fe / "icons" / "icon-192.png", media_type="image/png",
                            headers={"Cache-Control": "private, max-age=86400"})

    # ---------------------------------------------------------------- child API
    @app.get("/api/me")
    def me(request: Request):
        i = ident(request)
        return JSONResponse({"is_parent": i.is_parent, "email": i.email if i.is_parent else None,
                             "csrf": csrf_token(settings.csrf_secret, i.email)},
                            headers={"Cache-Control": NO_STORE})

    @app.get("/api/config")
    def client_config():
        t = core.cfg.theme()
        tiles = []
        approved = core.cfg.approved_hosts()
        for tile in core.cfg.tiles()["tiles"]:
            if tile["hidden"]:
                continue
            item = {"id": tile["id"], "label": tile["label"], "icon": tile["icon"],
                    "action": tile["action"]}
            if tile["action"] == "open_url":
                # Default deny: a concise tile only shows while its site is approved.
                if urlsplit(tile["url"]).hostname not in approved:
                    continue
                item["url"] = tile["url"]
            tiles.append(item)
        return JSONResponse({"site_name": t["site_name"], "tagline": t["tagline"],
                             "wordmark": t["wordmark"], "tiles": tiles},
                            headers={"Cache-Control": NO_STORE})

    @app.get("/api/search")
    def api_search(request: Request, q: str = "", type: str = "all", page: int = 1, tile: str = ""):
        i = ident(request)
        if len(q) > MAX_QUERY:
            return _err(400, f"Searches can be up to {MAX_QUERY} letters long.")
        if type not in TYPES:
            type = "all"
        page = max(1, min(int(page), 50))
        if not core.limiter.allow("search", i.email, settings.search_rate_per_min):
            return _err(429, "Whoa, that's a lot of searching! Take a breath and try again in a minute.")
        q = normalize_query(q)
        return JSONResponse(core.search.search(q, type, page, tile or None),
                            headers={"Cache-Control": NO_STORE})

    @app.get("/api/suggest")
    def api_suggest(request: Request, q: str = ""):
        i = ident(request)
        if len(q) > MAX_QUERY:
            return JSONResponse({"suggestions": []})
        if not core.limiter.allow("suggest", i.email, 240):
            return JSONResponse({"suggestions": []})
        return JSONResponse({"suggestions": core.search.suggest(q)})

    @app.get("/api/curious")
    def api_curious(request: Request, tile: str = ""):
        i = ident(request)
        if not core.limiter.allow("search", i.email, settings.search_rate_per_min):
            return _err(429, "Take a breath and try again in a minute.")
        hosts = None
        if tile:
            t = core.search.tile(tile)
            if t is None or t["action"] != "random":
                return _err(404, "not found")
            hosts = t["hosts"]
        url = core.search.random_page(hosts)
        if not url:
            return _err(404, "Nothing to explore yet — the library is still being built.")
        return JSONResponse({"url": url})

    @app.post("/api/requests")
    async def api_request(request: Request):
        i = ident(request)
        body = await json_body(request, 4096)
        text = normalize_query(str(body.get("text", "")))[:MAX_QUERY]
        kind = body.get("kind")
        if kind not in ("query", "site") or not text:
            raise ConfigError("Bad request.")
        if not core.limiter.allow("ask", i.email, 10):
            return _err(429, "You've asked a lot already — a grown-up will look soon!")
        with core.db.tx() as c:
            dup = c.execute("SELECT 1 FROM requests WHERE status='new' AND kind=? AND text=?",
                            (kind, text)).fetchone()
            if not dup:
                c.execute("INSERT INTO requests(ts, kind, text) VALUES (?,?,?)", (now_iso(), kind, text))
        return JSONResponse({"ok": True})

    @app.get("/api/photos/{opaque}/{kind}")
    def api_photo(request: Request, opaque: str, kind: str):
        i = ident(request)
        if kind not in ("thumb", "view"):
            return _err(404, "not found")
        if not core.limiter.allow("thumb", i.email, settings.thumb_rate_per_min):
            return _err(429, "slow down")
        data = core.photos.image(opaque, kind)
        if data is None:
            return _err(404, "not found")
        return Response(data, media_type="image/jpeg",
                        headers={"Cache-Control": NO_STORE, "Content-Disposition": "inline"})

    @app.get("/api/library/{lid}")
    def api_library_item(lid: str):
        row = core.library.get(lid)
        if row is None:
            return _err(404, "not found")
        out = {"id": row["id"], "kind": row["kind"], "title": row["title"], "note": row["note"]}
        if row["kind"] == "url":
            out["url"] = row["url"]
        if row["kind"] == "pdf":
            out["file"] = f"/api/library/{row['id']}/file"
        return JSONResponse(out)

    @app.get("/api/library/{lid}/file")
    def api_library_file(lid: str):
        p = core.library.pdf_path(core.library.get(lid))
        if p is None:
            return _err(404, "not found")
        return FileResponse(p, media_type="application/pdf", headers={
            "Content-Security-Policy": PDF_CSP,
            "Content-Disposition": 'inline; filename="library.pdf"',
            "Cache-Control": NO_STORE,
        })

    # ---------------------------------------------------------------- parent API
    @app.get("/api/parent/state")
    def p_state():
        lib = core.library.all()
        st = core.parent.state(settings.cf_team_domain, public_host, lib)
        st["library"] = [{k: r[k] for k in ("id", "kind", "title", "note", "url", "host", "file_size",
                                            "created", "created_by")} for r in lib]
        with core.db.conn() as c:
            st["history_count"] = c.execute("SELECT COUNT(*) FROM search_history").fetchone()[0]
        st["immich_configured"] = settings.immich_configured
        counts = {}
        for t in st["tiles"]["tiles"]:
            try:
                counts[t["id"]] = core.search.tile_count(t)
            except MeiliError:
                counts[t["id"]] = None
        st["tile_counts"] = counts
        approved = core.cfg.approved_hosts()
        st["tile_links"] = {t["id"]: {"url": t["url"],
                                      "approved": urlsplit(t["url"]).hostname in approved}
                            for t in st["tiles"]["tiles"] if t.get("url")}
        st["tiles"] = {**st["tiles"], "tiles": [
            {**t, "sections": [f"https://{k}" for k in t.get("sections") or []]}
            for t in st["tiles"]["tiles"]]}
        return JSONResponse(st)

    @app.post("/api/parent/sites")
    async def p_sites(request: Request):
        body = await json_body(request)
        return JSONResponse(core.parent.site_action(ident(request).email, body))

    @app.post("/api/parent/crawl")
    async def p_crawl(request: Request):
        body = await json_body(request)
        hosts = body.get("hosts")
        if hosts is not None and not isinstance(hosts, list):
            raise ConfigError("Bad request.")
        core.parent.request_crawl(ident(request).email, hosts)
        return JSONResponse({"ok": True})

    @app.post("/api/parent/familylink")
    async def p_familylink(request: Request):
        body = await json_body(request)
        core.parent.familylink(ident(request).email, body.get("host", ""), bool(body.get("confirmed")))
        return JSONResponse({"ok": True})

    @app.post("/api/parent/library")
    async def p_library(request: Request):
        body = await json_body(request, 32 * 1024)
        actor = ident(request).email
        action = body.get("action")
        if action == "add_url":
            row = core.library.add_url(actor, str(body.get("url", "")), str(body.get("title", "")),
                                       str(body.get("note", "")))
        elif action == "add_note":
            row = core.library.add_note(actor, str(body.get("title", "")), str(body.get("note", "")))
        elif action == "remove":
            core.library.remove(actor, str(body.get("id", "")))
            return JSONResponse({"ok": True})
        else:
            raise ConfigError("Unknown library action.")
        return JSONResponse({"id": row["id"]})

    @app.post("/api/parent/library/pdf")
    async def p_library_pdf(request: Request):
        cl = request.headers.get("content-length")
        if cl is None or not cl.isdigit() or int(cl) > MAX_PDF + 64 * 1024:
            raise ConfigError("PDFs can be up to 25 MB.")
        form = await request.form(max_files=1, max_fields=4)
        up = form.get("file")
        if up is None or not hasattr(up, "read"):
            raise ConfigError("Choose a PDF file.")
        data = await up.read(MAX_PDF + 1)
        await up.close()
        row = core.library.add_pdf(ident(request).email, str(form.get("title", "")),
                                   str(form.get("note", "")), data)
        return JSONResponse({"id": row["id"]})

    @app.post("/api/parent/albums")
    async def p_albums(request: Request):
        body = await json_body(request)
        return JSONResponse(core.parent.album_action(ident(request).email, body))

    @app.post("/api/parent/albums/sync")
    def p_albums_sync(request: Request):
        if not core._sync_lock.acquire(blocking=False):
            return _err(409, "A sync is already running.")
        try:
            return JSONResponse(core.photos.sync(ident(request).email))
        except ImmichError as e:
            return _err(502, str(e))
        finally:
            core._sync_lock.release()

    @app.post("/api/parent/requests")
    async def p_requests(request: Request):
        core.parent.request_action(ident(request).email, await json_body(request))
        return JSONResponse({"ok": True})

    @app.post("/api/parent/filters")
    async def p_filters(request: Request):
        core.parent.save_filters(ident(request).email, await json_body(request))
        return JSONResponse({"ok": True})

    @app.post("/api/parent/tiles")
    async def p_tiles(request: Request):
        core.parent.save_tiles(ident(request).email, await json_body(request))
        return JSONResponse({"ok": True})

    @app.post("/api/parent/settings")
    async def p_settings(request: Request):
        core.parent.save_settings(ident(request).email, await json_body(request))
        return JSONResponse({"ok": True})

    @app.get("/api/parent/audit")
    def p_audit(kind: str = "", limit: int = 200):
        kind = kind if kind in ("parent", "crawl", "sync", "denied", "system") else ""
        core.parent.ingest_crawl_status()
        return JSONResponse({"rows": core.db.audit_rows(max(1, min(limit, 1000)), kind or None)})

    @app.get("/api/parent/history")
    def p_history():
        with core.db.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT ts, q, type FROM search_history ORDER BY id DESC LIMIT 500")]
        return JSONResponse({"rows": rows})

    @app.post("/api/parent/history/clear")
    def p_history_clear(request: Request):
        with core.db.conn() as c:
            c.execute("DELETE FROM search_history")
        core.db.audit(ident(request).email, "parent", "search history cleared")
        return JSONResponse({"ok": True})

    @app.api_route("/{rest:path}", methods=["GET", "HEAD", "POST"])
    def not_found(rest: str):
        return _err(404, "not found")

    return app


mimetypes.add_type("application/manifest+json", ".webmanifest")
