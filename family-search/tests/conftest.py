from __future__ import annotations

import io
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from PIL import Image
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "crawler"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakemeili import FakeMeili  # noqa: E402
from familysearch.auth import AccessVerifier  # noqa: E402
from familysearch.config import load_settings  # noqa: E402
from familysearch.main import create_app  # noqa: E402
from familysearch.meili import Meili  # noqa: E402
from familysearch.photos import Immich  # noqa: E402

TEAM = "strzfamily.cloudflareaccess.com"
AUD = "a" * 64
ORIGIN = "https://search.thestrzs.net"
PARENT = "david@example.com"
CHILD = "kid@example.com"
IMMICH_URL = "http://192.168.0.101:2283"
IMMICH_KEY = "immich-secret-key-DO-NOT-LEAK"
ALBUM_A = "11111111-1111-4111-8111-111111111111"
ALBUM_B = "22222222-2222-4222-8222-222222222222"


class Keys:
    def __init__(self):
        self.priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.priv.public_key(), as_dict=True)
        jwk.update(kid="k1", alg="RS256", use="sig")
        self.jwks = {"keys": [jwk]}
        self.fetches = 0

    def fetch(self, url):
        assert url == f"https://{TEAM}/cdn-cgi/access/certs"
        self.fetches += 1
        return self.jwks

    def token(self, email=CHILD, aud=AUD, iss=f"https://{TEAM}", exp_in=3600, key=None,
              kid="k1", alg="RS256", extra=None):
        now = int(time.time())
        claims = {"email": email, "aud": [aud], "iss": iss, "iat": now, "nbf": now,
                  "exp": now + exp_in, "sub": "x", "type": "app"}
        if email is None:
            claims.pop("email")
        claims.update(extra or {})
        return jwt.encode(claims, key or self.priv, algorithm=alg, headers={"kid": kid})


@pytest.fixture(scope="session")
def keys():
    return Keys()


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    data = tmp_path / "data"
    (data / "spool").mkdir(parents=True)
    vals = {
        "FS_ROOT": str(ROOT), "FS_CONFIG_DIR": str(cfg), "FS_DATA_DIR": str(data),
        "FS_FRONTEND_DIR": str(ROOT / "frontend"), "FS_PUBLIC_ORIGIN": ORIGIN,
        "CF_ACCESS_TEAM_DOMAIN": TEAM, "CF_ACCESS_AUD": AUD,
        "PARENT_EMAILS": f"{PARENT}, mom@example.com", "MEILI_URL": "http://127.0.0.1:7700",
        "MEILI_MASTER_KEY": "meili-test-key", "CSRF_SECRET": "csrf-test-secret",
        "IMMICH_URL": IMMICH_URL, "IMMICH_API_KEY": IMMICH_KEY,
    }
    for k, v in vals.items():
        monkeypatch.setenv(k, v)
    return tmp_path


def jpeg_with_gps() -> bytes:
    img = Image.new("RGB", (800, 600), (200, 120, 90))
    exif = Image.Exif()
    exif[0x010F] = "SecretCamCo"          # Make
    exif[0x0110] = "Model X"              # Model
    gps = {1: "N", 2: (42.0, 30.0, 15.0), 3: "W", 4: (83.0, 10.0, 5.0)}
    exif[0x8825] = gps
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif.tobytes())
    return buf.getvalue()


class FakeImmich:
    """Records every path requested so tests can assert nothing else was hit."""

    def __init__(self):
        self.paths: list[str] = []
        self.albums = {
            ALBUM_A: {"albumName": "Beach 2025", "assets": [
                {"id": "aaaaaaaa-0000-4000-8000-00000000000%d" % i, "type": "IMAGE",
                 "localDateTime": f"2025-07-0{i}T10:00:00.000Z",
                 "exifInfo": {"description": f"sandcastle {i}", "latitude": 42.5, "longitude": -83.1,
                              "city": "Secretville"}} for i in range(1, 4)
            ] + [{"id": "aaaaaaaa-0000-4000-8000-0000000000ff", "type": "VIDEO",
                  "localDateTime": "2025-07-05T10:00:00.000Z"}]},
            ALBUM_B: {"albumName": "Recital", "assets": [
                {"id": "bbbbbbbb-0000-4000-8000-000000000001", "type": "IMAGE",
                 "localDateTime": "2025-05-01T18:00:00.000Z", "exifInfo": {"description": "ballet bow"}}]},
        }

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request):
        self.paths.append(req.url.path)
        assert req.headers.get("x-api-key") == IMMICH_KEY
        p = req.url.path
        if p.startswith("/api/albums/"):
            a = self.albums.get(p.rsplit("/", 1)[1])
            return httpx.Response(200, json={**a, "assetCount": len(a["assets"])}) if a else httpx.Response(404)
        if p.endswith("/thumbnail"):
            return httpx.Response(200, content=jpeg_with_gps(), headers={"content-type": "image/jpeg"})
        if p == "/api/server/version":
            return httpx.Response(200, json={"major": 1, "minor": 140, "patch": 0})
        return httpx.Response(404)


@pytest.fixture
def fake_meili():
    return FakeMeili()


@pytest.fixture
def fake_immich():
    return FakeImmich()


@pytest.fixture
def make_app(env, keys, fake_meili, fake_immich):
    def _make(**overrides):
        s = replace(load_settings(), **overrides)
        verifier = AccessVerifier(TEAM, AUD, fetch_jwks=keys.fetch) if s.auth_configured else None
        immich = Immich(IMMICH_URL, IMMICH_KEY, transport=fake_immich.transport())
        meili = Meili("http://127.0.0.1:7700", "k", transport=fake_meili.transport())
        return create_app(s, verifier=verifier, immich=immich, meili=meili)
    return _make


class Client:
    """TestClient wrapper that signs in as someone and tracks every response body."""

    def __init__(self, app, keys, email):
        self.tc = TestClient(app, base_url=ORIGIN)
        self.tc.__enter__()
        self.keys = keys
        self.email = email
        self.bodies: list[bytes] = []
        self.csrf = None
        if email:
            r = self.get("/api/me")
            self.csrf = r.json().get("csrf")

    def headers(self, extra=None):
        h = {}
        if self.email:
            h["Cf-Access-Jwt-Assertion"] = self.keys.token(self.email)
        h.update(extra or {})
        return h

    def get(self, path, headers=None, **kw):
        r = self.tc.get(path, headers=self.headers(headers), **kw)
        self.bodies.append(r.content)
        return r

    def post(self, path, json=None, headers=None, csrf=True, origin=ORIGIN, **kw):
        h = {}
        if origin:
            h["Origin"] = origin
        if csrf and self.csrf:
            h["X-CSRF-Token"] = self.csrf
        h.update(headers or {})
        r = self.tc.post(path, json=json, headers=self.headers(h), **kw)
        self.bodies.append(r.content)
        return r


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def child(app, keys):
    return Client(app, keys, CHILD)


@pytest.fixture
def parent(app, keys):
    return Client(app, keys, PARENT)


@pytest.fixture
def core(app):
    return app.state.core
