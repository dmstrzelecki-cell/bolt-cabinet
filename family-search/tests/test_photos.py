"""Acceptance: Photos."""
from __future__ import annotations

import io
import re

from PIL import Image

from conftest import ALBUM_A, ALBUM_B, IMMICH_KEY, IMMICH_URL, CHILD, Client
from familysearch.photos import ALLOWED_PATHS, Immich, ImmichError


def allow_albums(parent, *ids):
    for i in ids:
        assert parent.post("/api/parent/albums", json={"action": "add", "id": i}).status_code == 200
    r = parent.post("/api/parent/albums/sync", json={})
    assert r.status_code == 200, r.text
    return r.json()


def test_only_photos_in_approved_albums_appear(parent, child, fake_immich):
    s = allow_albums(parent, ALBUM_A)
    assert s["photos"] == 3 and s["errors"] == []  # the video is excluded
    d = child.get("/api/search?type=photos&q=").json()
    assert len(d["results"]) == 3
    assert all(r["album"] == "Beach 2025" for r in d["results"])
    assert child.get("/api/search?type=photos&q=ballet").json()["results"] == []  # album B not allowed
    # album B exists in Immich and is shared — still invisible until a parent allows it
    assert ALBUM_B not in "".join(fake_immich.paths)


def test_photo_search_by_caption_album_and_date(parent, child):
    allow_albums(parent, ALBUM_A, ALBUM_B)
    assert len(child.get("/api/search?type=photos&q=sandcastle").json()["results"]) == 3
    assert len(child.get("/api/search?type=photos&q=recital").json()["results"]) == 1
    assert len(child.get("/api/search?type=photos&q=july 2025").json()["results"]) == 3
    strip = child.get("/api/search?q=sandcastle").json()["photos"]
    assert 1 <= len(strip) <= 8


def test_browser_sees_only_opaque_ids(parent, child):
    allow_albums(parent, ALBUM_A)
    d = child.get("/api/search?type=photos&q=").json()
    blob = str(d)
    for r in d["results"]:
        assert re.fullmatch(r"[A-Za-z0-9_-]{22}", r["id"])
        assert set(r) == {"kind", "id", "album", "date", "description"}
    assert "aaaaaaaa-0000" not in blob and ALBUM_A not in blob
    assert "42.5" not in blob and "Secretville" not in blob


def test_guessed_opaque_id_returns_404(parent, child):
    allow_albums(parent, ALBUM_A)
    for guess in ["AAAAAAAAAAAAAAAAAAAAAA", "aaaaaaaa-0000-4000-8000-000000000001", "../../etc/passwd",
                  "1", "x" * 200]:
        assert child.get(f"/api/photos/{guess}/thumb").status_code == 404, guess


def test_removing_an_album_immediately_404s_its_thumbnails(parent, child, core):
    allow_albums(parent, ALBUM_A)
    pid = child.get("/api/search?type=photos&q=").json()["results"][0]["id"]
    assert child.get(f"/api/photos/{pid}/thumb").status_code == 200
    assert parent.post("/api/parent/albums", json={"action": "remove", "id": ALBUM_A}).status_code == 200
    assert child.get(f"/api/photos/{pid}/thumb").status_code == 404
    assert child.get(f"/api/photos/{pid}/view").status_code == 404
    assert child.get("/api/search?type=photos&q=").json()["results"] == []


def test_album_disabled_by_hand_edit_is_revoked_even_with_stale_rows(parent, child, core):
    allow_albums(parent, ALBUM_A)
    pid = child.get("/api/search?type=photos&q=").json()["results"][0]["id"]
    # someone edits photo_albums.yaml by hand; SQLite still has the rows
    (core.s.config_dir / "photo_albums.yaml").write_text("albums: []\n")
    assert child.get(f"/api/photos/{pid}/thumb").status_code == 404


def test_thumbnails_have_no_exif_or_gps(parent, child):
    allow_albums(parent, ALBUM_A)
    pid = child.get("/api/search?type=photos&q=").json()["results"][0]["id"]
    for kind in ("thumb", "view"):
        r = child.get(f"/api/photos/{pid}/{kind}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        assert r.headers["cache-control"] == "private, no-store"
        img = Image.open(io.BytesIO(r.content))
        assert len(img.getexif()) == 0
        assert "exif" not in img.info and "icc_profile" not in img.info
        for needle in (b"Exif", b"SecretCamCo", b"GPS", b"Model X"):
            assert needle not in r.content
        assert max(img.size) <= (400 if kind == "thumb" else 1600)


def test_only_permitted_immich_endpoints_are_called(parent, child, fake_immich):
    allow_albums(parent, ALBUM_A, ALBUM_B)
    for r in child.get("/api/search?type=photos&q=").json()["results"]:
        child.get(f"/api/photos/{r['id']}/thumb")
        child.get(f"/api/photos/{r['id']}/view")
    assert fake_immich.paths
    for p in fake_immich.paths:
        assert any(rx.match(p) for rx in ALLOWED_PATHS), p
        for banned in ("original", "faces", "people", "person", "map", "location", "download"):
            assert banned not in p
    im = Immich(IMMICH_URL, IMMICH_KEY, transport=fake_immich.transport())
    for bad in ["/api/assets/aaaaaaaa-0000-4000-8000-000000000001/original", "/api/people",
                "/api/map/markers", "/api/faces"]:
        try:
            im._call("GET", bad)
            raise AssertionError(f"{bad} was allowed")
        except ImmichError:
            pass


def test_no_immich_details_reach_the_browser(app, keys, parent, fake_immich):
    allow_albums(parent, ALBUM_A)
    c = Client(app, keys, CHILD)
    d = c.get("/api/search?type=photos&q=").json()
    for r in d["results"]:
        c.get(f"/api/photos/{r['id']}/thumb")
    c.get("/api/search?q=sandcastle")
    blob = b"\n".join(c.bodies + parent.bodies)
    for needle in [IMMICH_KEY.encode(), IMMICH_URL.encode(), b"192.168.0.101", b"x-api-key",
                   b"aaaaaaaa-0000-4000", b"/api/assets/", b"latitude", b"Secretville"]:
        assert needle not in blob, needle


def test_photo_rate_limit(make_app, keys, parent):
    app = make_app(thumb_rate_per_min=3)
    c = Client(app, keys, CHILD)
    codes = [c.get("/api/photos/AAAAAAAAAAAAAAAAAAAAAA/thumb").status_code for _ in range(4)]
    assert codes == [404, 404, 404, 429]
