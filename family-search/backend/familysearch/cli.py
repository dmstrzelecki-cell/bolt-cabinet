"""Maintenance commands run by systemd timers and scripts/*.sh.

    python -m familysearch.cli ensure-indexes
    python -m familysearch.cli sync-photos
    python -m familysearch.cli reindex-local      # library + photos from SQLite
    python -m familysearch.cli check-immich       # version + permission probe
"""
from __future__ import annotations

import sys

from .config import ConfigStore, load_settings
from .db import DB
from .library import LibraryService
from .meili import LIBRARY, PHOTOS, Meili
from .photos import Immich, ImmichError, PhotoService


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    cmd = argv[0]
    s = load_settings()
    cfg = ConfigStore(s)
    db = DB(s.db_path)
    meili = Meili(s.meili_url, s.meili_key, timeout=60)
    immich = Immich(s.immich_url, s.immich_api_key) if s.immich_configured else None
    photos = PhotoService(cfg, db, meili, immich)

    if cmd == "ensure-indexes":
        for t in meili.ensure_indexes(cfg.filters()["synonyms"]):
            meili.wait(t)
        print("indexes ready")
    elif cmd == "sync-photos":
        if immich is None:
            print("Immich not configured yet (IMMICH_URL / IMMICH_API_KEY are placeholders) — skipping.")
            return 0
        r = photos.sync("timer")
        print(f"albums={r['albums']} photos={r['photos']} added={r['added']} "
              f"removed={r['removed']} errors={len(r['errors'])}")
        return 1 if r["errors"] else 0
    elif cmd == "reindex-local":
        for t in meili.ensure_indexes(cfg.filters()["synonyms"]):
            meili.wait(t)
        n_lib = LibraryService(db, meili, s.library_dir).reindex()
        n_ph = photos.reindex()
        print(f"{LIBRARY}: {n_lib} items, {PHOTOS}: {n_ph} photos queued for indexing")
    elif cmd == "check-immich":
        if immich is None:
            print("Immich not configured yet.")
            return 1
        try:
            print("Immich version:", immich.version())
        except ImmichError as e:
            print("version check failed:", e)
            return 1
        for a in cfg.albums():
            try:
                name, assets = immich.album_assets(a["id"])
                imgs = [x for x in assets if x.get("type") == "IMAGE"]
                print(f"album {a['id']}: '{name}' {len(imgs)} images  (album.read OK)")
                if imgs:
                    immich.rendition(imgs[0]["id"], "thumbnail")
                    print("  thumbnail fetch OK (asset.view)")
            except ImmichError as e:
                print(f"album {a['id']}: FAILED — {e}")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
