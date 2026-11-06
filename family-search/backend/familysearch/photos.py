"""Family photos from Immich, through a restricted, album-limited account.

Rules this module enforces:
  * Only the family-search Immich user's API key is used.
  * Only the endpoints in ALLOWED_PATHS are ever requested: album contents,
    metadata search scoped to an album, and the thumbnail/preview renditions.
    Never originals, faces, people, map, or location endpoints.
  * Immich asset ids never leave the server. The browser only sees random
    opaque ids, and every thumbnail request re-checks that the opaque id still
    exists and its album is still enabled in photo_albums.yaml.
  * Every image is decoded and re-encoded with Pillow, so no EXIF/GPS/XMP/ICC
    from the original ever reaches the browser.
"""
from __future__ import annotations

import io
import re
import secrets
from datetime import datetime

import httpx
from PIL import Image, ImageOps

from .config import ConfigStore
from .db import DB, now_iso
from .meili import PHOTOS, Meili
from .search import clean_text

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
ALLOWED_PATHS = [
    re.compile(rf"^/api/albums/{UUID}$"),
    re.compile(r"^/api/search/metadata$"),
    re.compile(rf"^/api/assets/{UUID}/thumbnail$"),
    re.compile(r"^/api/server/version$"),
]
OPAQUE_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
SIZES = {"thumb": ("thumbnail", 400), "view": ("preview", 1600)}
Image.MAX_IMAGE_PIXELS = 60_000_000


class ImmichError(RuntimeError):
    pass


class Immich:
    def __init__(self, base_url: str, api_key: str, transport: httpx.BaseTransport | None = None):
        self._c = httpx.Client(base_url=base_url, timeout=20, follow_redirects=False,
                               trust_env=False, transport=transport,
                               headers={"x-api-key": api_key, "Accept": "application/json"})

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        if not any(p.match(path) for p in ALLOWED_PATHS):
            raise ImmichError("endpoint not permitted")
        try:
            r = self._c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ImmichError(f"photo server unreachable ({type(e).__name__})") from None
        if r.status_code == 404:
            raise ImmichError("not found")
        if r.status_code >= 400:
            raise ImmichError(f"photo server returned {r.status_code}")
        return r

    def version(self) -> str:
        d = self._call("GET", "/api/server/version").json()
        return f"{d.get('major')}.{d.get('minor')}.{d.get('patch')}"

    def album_assets(self, album_id: str) -> tuple[str, list[dict]]:
        """(album name, image assets). Falls back to album-scoped metadata
        search on Immich versions whose album endpoint omits the asset list."""
        d = self._call("GET", f"/api/albums/{album_id}").json()
        name = str(d.get("albumName") or "")
        assets = d.get("assets")
        if not assets and int(d.get("assetCount") or 0) > 0:
            assets, page = [], 1
            while page and page <= 200:
                r = self._call("POST", "/api/search/metadata", json={
                    "albumIds": [album_id], "type": "IMAGE", "page": page, "size": 500,
                    "withExif": True,
                }).json()
                block = (r or {}).get("assets") or {}
                assets.extend(block.get("items") or [])
                nxt = block.get("nextPage")
                page = int(nxt) if nxt else 0
        return name, list(assets or [])

    def rendition(self, asset_id: str, size: str) -> bytes:
        r = self._call("GET", f"/api/assets/{asset_id}/thumbnail", params={"size": size})
        if len(r.content) > 25 * 1024 * 1024:
            raise ImmichError("image too large")
        return r.content


def _asset_fields(a: dict) -> dict | None:
    if a.get("type") != "IMAGE" or a.get("isTrashed") or a.get("isOffline"):
        return None
    aid = str(a.get("id", ""))
    if not re.fullmatch(UUID, aid):
        return None
    raw_date = a.get("localDateTime") or a.get("fileCreatedAt") or ""
    taken = ""
    try:
        taken = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    exif = a.get("exifInfo") or {}
    # Only the human-written caption. Never GPS, camera, city/state/country, etc.
    desc = clean_text(exif.get("description") or "", 300)
    return {"immich_id": aid, "taken_date": taken, "description": desc}


def date_text(d: str) -> str:
    if not d:
        return ""
    try:
        dt = datetime.fromisoformat(d)
    except ValueError:
        return ""
    season = ("winter", "winter", "spring", "spring", "spring", "summer", "summer",
              "summer", "fall", "fall", "fall", "winter")[dt.month - 1]
    return f"{dt.year} {dt.strftime('%B')} {dt.day} {dt.strftime('%A')} {season}"


def photo_doc(opaque: str, album_id: str, album_name: str, taken: str, desc: str) -> dict:
    return {"id": opaque, "album_id": album_id, "album_name": album_name,
            "taken_date": taken, "date_text": date_text(taken), "description": desc}


class PhotoService:
    def __init__(self, cfg: ConfigStore, db: DB, meili: Meili, immich: Immich | None):
        self.cfg = cfg
        self.db = db
        self.meili = meili
        self.immich = immich

    # -- sync ------------------------------------------------------------------
    def sync(self, actor: str = "system") -> dict:
        if self.immich is None:
            raise ImmichError("Immich is not configured yet (IMMICH_URL / IMMICH_API_KEY)")
        albums = [a for a in self.cfg.albums() if a["enabled"]]
        enabled = {a["id"] for a in albums}
        summary = {"albums": 0, "photos": 0, "added": 0, "removed": 0, "errors": []}
        removed_ids: list[str] = []
        docs: list[dict] = []

        # Revoke anything not currently enabled first, whatever happens next.
        removed_ids += self.revoke_disabled()

        for a in albums:
            try:
                name, assets = self.immich.album_assets(a["id"])
            except ImmichError as e:
                summary["errors"].append({"album": a["id"], "error": str(e)})
                with self.db.conn() as c:
                    c.execute("""INSERT INTO albums_state(album_id, name, last_error) VALUES (?,?,?)
                                 ON CONFLICT(album_id) DO UPDATE SET last_error=excluded.last_error""",
                              (a["id"], a.get("name") or "", str(e)))
                continue
            album_name = clean_text(a.get("name") or name, 80)
            fields = [f for f in (_asset_fields(x) for x in assets) if f]
            wanted = {f["immich_id"]: f for f in fields}
            with self.db.tx() as c:
                existing = {r["immich_id"]: r["opaque_id"] for r in c.execute(
                    "SELECT immich_id, opaque_id FROM photos WHERE album_id=?", (a["id"],))}
                gone = [existing[i] for i in existing if i not in wanted]
                if gone:
                    c.executemany("DELETE FROM photos WHERE opaque_id=?", [(g,) for g in gone])
                    removed_ids += gone
                for iid, f in wanted.items():
                    opaque = existing.get(iid)
                    if opaque is None:
                        opaque = secrets.token_urlsafe(16)
                        summary["added"] += 1
                        c.execute("""INSERT INTO photos(opaque_id, immich_id, album_id, taken_date,
                                     description, synced_at) VALUES (?,?,?,?,?,?)""",
                                  (opaque, iid, a["id"], f["taken_date"], f["description"], now_iso()))
                    else:
                        c.execute("""UPDATE photos SET taken_date=?, description=?, synced_at=?
                                     WHERE opaque_id=?""",
                                  (f["taken_date"], f["description"], now_iso(), opaque))
                    docs.append(photo_doc(opaque, a["id"], album_name, f["taken_date"], f["description"]))
                c.execute("""INSERT INTO albums_state(album_id, name, photo_count, last_sync, last_error)
                             VALUES (?,?,?,?,NULL)
                             ON CONFLICT(album_id) DO UPDATE SET name=excluded.name,
                               photo_count=excluded.photo_count, last_sync=excluded.last_sync,
                               last_error=NULL""",
                          (a["id"], album_name, len(wanted), now_iso()))
            summary["albums"] += 1
            summary["photos"] += len(wanted)

        summary["removed"] = len(removed_ids)
        self.meili.delete_documents(PHOTOS, removed_ids)
        for i in range(0, len(docs), 1000):
            self.meili.add_documents(PHOTOS, docs[i:i + 1000])
        self.db.audit(actor, "sync", "photos synced",
                      {k: v for k, v in summary.items() if k != "errors"} |
                      {"errors": len(summary["errors"]), "enabled_albums": len(enabled)})
        return summary

    def revoke_disabled(self) -> list[str]:
        """Drop every photo whose album is no longer enabled. Immediate 404s."""
        enabled = self.cfg.enabled_album_ids()
        with self.db.tx() as c:
            rows = c.execute("SELECT opaque_id, album_id FROM photos").fetchall()
            gone = [r["opaque_id"] for r in rows if r["album_id"] not in enabled]
            c.executemany("DELETE FROM photos WHERE opaque_id=?", [(g,) for g in gone])
            c.execute(f"DELETE FROM albums_state WHERE album_id NOT IN ({','.join('?' * len(enabled)) or 'NULL'})",
                      list(enabled))
        if gone:
            try:
                self.meili.delete_documents(PHOTOS, gone)
            except Exception:
                pass  # search results are filtered against SQLite anyway
        return gone

    def reindex(self) -> int:
        """Push every allowed photo from SQLite into the index (rebuild)."""
        enabled = self.cfg.enabled_album_ids()
        names = {a["id"]: a.get("name") or "" for a in self.cfg.albums()}
        with self.db.conn() as c:
            for r in c.execute("SELECT album_id, name FROM albums_state"):
                names[r["album_id"]] = names.get(r["album_id"]) or r["name"] or ""
            rows = c.execute("SELECT * FROM photos").fetchall()
        docs = [photo_doc(r["opaque_id"], r["album_id"], names.get(r["album_id"], ""),
                          r["taken_date"] or "", r["description"] or "")
                for r in rows if r["album_id"] in enabled]
        for i in range(0, len(docs), 1000):
            self.meili.add_documents(PHOTOS, docs[i:i + 1000])
        return len(docs)

    # -- serving ---------------------------------------------------------------
    def lookup(self, opaque: str) -> str | None:
        """Immich id for an opaque id, only if it is allowed right now."""
        if not OPAQUE_RE.match(opaque or ""):
            return None
        with self.db.conn() as c:
            r = c.execute("SELECT immich_id, album_id FROM photos WHERE opaque_id=?", (opaque,)).fetchone()
        if r is None or r["album_id"] not in self.cfg.enabled_album_ids():
            return None
        return r["immich_id"]

    def image(self, opaque: str, kind: str) -> bytes | None:
        if kind not in SIZES or self.immich is None:
            return None
        iid = self.lookup(opaque)
        if iid is None:
            return None
        size, max_px = SIZES[kind]
        try:
            raw = self.immich.rendition(iid, size)
        except ImmichError:
            return None
        return reencode(raw, max_px)


def reencode(raw: bytes, max_px: int) -> bytes:
    """Decode, apply orientation, and write a brand-new JPEG with no metadata."""
    with Image.open(io.BytesIO(raw)) as src:
        src.load()
        img = ImageOps.exif_transpose(src)
        img = img.convert("RGB")
        img.thumbnail((max_px, max_px))
        clean = Image.new("RGB", img.size)
        clean.paste(img)
    out = io.BytesIO()
    clean.save(out, "JPEG", quality=82, optimize=True, progressive=True)
    return out.getvalue()
