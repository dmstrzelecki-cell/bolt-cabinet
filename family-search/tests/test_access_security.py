"""Acceptance: Access and security."""
from __future__ import annotations

import time

import jwt
from starlette.testclient import TestClient

from conftest import AUD, CHILD, ORIGIN, PARENT, TEAM

REQUIRED_HEADERS = {
    "content-security-policy": ["default-src 'self'", "frame-ancestors 'none'"],
    "referrer-policy": ["no-referrer"],
    "x-content-type-options": ["nosniff"],
    "permissions-policy": ["camera=()", "geolocation=()", "microphone=()"],
}


def raw(app):
    return TestClient(app, base_url=ORIGIN)


def test_no_jwt_is_401_everywhere(app):
    c = raw(app)
    for path in ["/", "/parent", "/api/me", "/api/search?q=cats", "/static/app.js",
                 "/theme.css", "/manifest.webmanifest", "/api/photos/x/thumb", "/nope"]:
        r = c.get(path)
        assert r.status_code in (401, 403), path
        assert "cats" not in r.text


def test_forged_expired_wrong_audience_issuer_algorithm_rejected(app, keys):
    c = raw(app)
    bad = {
        "forged signature": keys.token(CHILD, key=keys.other),
        "expired": keys.token(CHILD, exp_in=-3600),
        "wrong audience": keys.token(CHILD, aud="b" * 64),
        "wrong issuer": keys.token(CHILD, iss="https://evil.cloudflareaccess.com"),
        "unknown kid": keys.token(CHILD, kid="nope"),
        "hs256 with public jwk": jwt.encode(
            {"email": CHILD, "aud": AUD, "iss": f"https://{TEAM}", "exp": int(time.time()) + 60,
             "iat": int(time.time())}, "secret", algorithm="HS256", headers={"kid": "k1"}),
        "garbage": "not.a.jwt",
        "no email (service token)": keys.token(None),
    }
    for name, tok in bad.items():
        r = c.get("/api/me", headers={"Cf-Access-Jwt-Assertion": tok})
        assert r.status_code in (401, 403), name


def test_valid_child_and_parent(child, parent):
    me = child.get("/api/me").json()
    assert me["is_parent"] is False and me["email"] is None and me["csrf"]
    assert parent.get("/api/me").json()["is_parent"] is True


def test_identity_header_spoofing_is_ignored(app, keys):
    c = raw(app)
    r = c.get("/api/me", headers={"Cf-Access-Authenticated-User-Email": PARENT,
                                  "X-Forwarded-User": PARENT, "Cf-Connecting-Ip": "1.2.3.4"})
    assert r.status_code == 401


def test_child_cannot_open_parent_page_or_apis(child):
    for path in ["/parent", "/api/parent/state", "/api/parent/audit", "/api/parent/history",
                 "/static/parent.js", "/static/parent.css"]:
        assert child.get(path).status_code == 403, path
    for path, body in [("/api/parent/sites", {"action": "add", "host": "evil.example", "category": "x"}),
                       ("/api/parent/albums", {"action": "add", "id": "11111111-1111-4111-8111-111111111111"}),
                       ("/api/parent/filters", {"blocked": []}),
                       ("/api/parent/settings", {"store_history": True}),
                       ("/api/parent/tiles", {"tiles": []})]:
        assert child.post(path, json=body).status_code == 403, path


def test_parent_can_open_parent_page(parent):
    assert parent.get("/parent").status_code == 200
    assert parent.get("/api/parent/state").status_code == 200


def test_denied_events_are_audited(app, child, parent):
    raw(app).get("/api/me")
    child.get("/api/parent/state")
    rows = parent.get("/api/parent/audit?kind=denied").json()["rows"]
    actions = {r["action"] for r in rows}
    assert "missing token" in actions and "child tried parent area" in actions
    for r in rows:
        assert "token" not in str(r["detail"]).lower() or r["action"] == "missing token"


def test_csrf_and_origin_required_for_writes(parent):
    body = {"store_history": True}
    assert parent.post("/api/parent/settings", json=body, csrf=False).status_code == 403
    assert parent.post("/api/parent/settings", json=body, origin="https://evil.example").status_code == 403
    assert parent.post("/api/parent/settings", json=body, origin=None).status_code == 403
    assert parent.post("/api/parent/settings", json=body,
                       headers={"X-CSRF-Token": "0" * 64}).status_code == 403
    assert parent.post("/api/parent/settings", json=body).status_code == 200


def test_csrf_token_is_bound_to_the_user(app, keys, child, parent):
    r = parent.post("/api/parent/settings", json={"store_history": True},
                    headers={"X-CSRF-Token": child.csrf})
    assert r.status_code == 403


def test_security_headers_on_every_response(app, child):
    responses = [child.get("/"), child.get("/api/search?q=space"), child.get("/static/app.js"),
                 child.get("/api/does-not-exist"), raw(app).get("/api/me"),
                 child.get("/parent")]
    for r in responses:
        for h, needles in REQUIRED_HEADERS.items():
            assert h in r.headers, (r.url, h)
            for n in needles:
                assert n in r.headers[h], (r.url, h, n)
        assert "x-frame-options" in r.headers


def test_no_store_on_search_parent_and_photo_responses(parent, child):
    for r in [child.get("/api/search?q=moon"), parent.get("/api/parent/state"),
              child.get("/api/photos/AAAAAAAAAAAAAAAAAAAAAA/thumb")]:
        assert r.headers["cache-control"] == "private, no-store", r.url


def test_unconfigured_app_fails_closed(make_app, keys):
    app = make_app(cf_aud="__SET_ME__")
    c = raw(app)
    r = c.get("/api/me", headers={"Cf-Access-Jwt-Assertion": keys.token(PARENT)})
    assert r.status_code == 503


def test_internal_services_not_reachable_through_app(child):
    # The app has no proxy or passthrough route: anything aimed at Meilisearch
    # or Immich paths is a plain 404 from the backend and never forwarded.
    for path in ["/indexes", "/indexes/web_pages/search", "/keys", "/api/albums",
                 "/api/assets/x/original", "/api/server/version", "/api/people",
                 "/api/map/markers", "/7700", "/2283"]:
        r = child.get(path)
        assert r.status_code == 404, path


def test_methods_other_than_get_post_refused(child):
    r = child.tc.request("PUT", "/api/search", headers=child.headers())
    assert r.status_code == 405
    r = child.tc.request("DELETE", "/api/parent/state", headers=child.headers())
    assert r.status_code == 405


def test_query_length_and_rate_limits(make_app, keys):
    from conftest import Client
    app = make_app(search_rate_per_min=5)
    c = Client(app, keys, CHILD)
    assert c.get("/api/search?q=" + "a" * 201).status_code == 400
    codes = [c.get("/api/search?q=moon").status_code for _ in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429


def test_docs_and_openapi_disabled(child):
    for p in ["/docs", "/redoc", "/openapi.json"]:
        assert child.get(p).status_code == 404
