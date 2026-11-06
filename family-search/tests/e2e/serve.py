"""Dev-only: run the real app with fake Meilisearch/Immich and a test signing
key, for the Playwright check in tests/e2e/check.mjs. Never used on the CT.

    python tests/e2e/serve.py <port> <tokens.json>
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "crawler"), str(HERE.parent)]

port = int(sys.argv[1])
tokens_out = Path(sys.argv[2])
tmp = Path(tempfile.mkdtemp(prefix="fs-e2e-"))
shutil.copytree(ROOT / "config", tmp / "config")
(tmp / "data" / "spool").mkdir(parents=True)
os.environ.update({
    "FS_ROOT": str(ROOT), "FS_CONFIG_DIR": str(tmp / "config"), "FS_DATA_DIR": str(tmp / "data"),
    "FS_FRONTEND_DIR": str(ROOT / "frontend"), "FS_PUBLIC_ORIGIN": f"http://127.0.0.1:{port}",
    "CF_ACCESS_TEAM_DOMAIN": "strzfamily.cloudflareaccess.com", "CF_ACCESS_AUD": "a" * 64,
    "PARENT_EMAILS": "david@example.com", "CSRF_SECRET": "x", "MEILI_MASTER_KEY": "k",
    "IMMICH_URL": "http://immich.invalid:2283", "IMMICH_API_KEY": "k",
})

import uvicorn  # noqa: E402

import conftest as C  # noqa: E402
from familysearch.auth import AccessVerifier  # noqa: E402
from familysearch.config import load_settings  # noqa: E402
from familysearch.main import create_app  # noqa: E402
from familysearch.meili import WEB, Meili  # noqa: E402
from familysearch.photos import Immich  # noqa: E402
from test_search_content import SEED, wdoc  # noqa: E402

keys = C.Keys()
fm = C.FakeMeili()
fi = C.FakeImmich()
(tmp / "config" / "photo_albums.yaml").write_text(f"albums:\n  - {{id: {C.ALBUM_A}, name: Beach 2025}}\n")
app = create_app(load_settings(), verifier=AccessVerifier(C.TEAM, C.AUD, fetch_jwks=keys.fetch),
                 immich=Immich("http://immich.invalid:2283", C.IMMICH_KEY, transport=fi.transport()),
                 meili=Meili("http://x", "k", transport=fm.transport()))
idx = fm.indexes.setdefault(WEB, {})
docs = list(SEED) + [
    wdoc("https://www.nasa.gov/xss", "<script>window.__pwned=1</script> Moon trick",
         "<img src=x onerror=window.__pwned=2> moon facts", category="space") | {"boost": 5}]
docs += [wdoc(f"https://simple.wikipedia.org/wiki/Moon_{i}", f"Moon fact {i}",
              "The moon is Earth's only natural satellite. " * 6) for i in range(25)]
for d in docs:
    idx[d["id"]] = d
app.state.core.photos.sync("e2e")
tokens_out.write_text(json.dumps({"child": keys.token(C.CHILD, exp_in=7200),
                                  "parent": keys.token("david@example.com", exp_in=7200)}))
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
