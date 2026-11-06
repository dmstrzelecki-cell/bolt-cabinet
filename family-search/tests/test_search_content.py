"""Acceptance: Search and content (+ tiles, library, rebuild)."""
from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

import yaml

from conftest import CHILD, Client
from familysearch.meili import LIBRARY, PHOTOS, WEB

PRE, POST = "", ""


def wdoc(url, title, body="", host=None, category="reference", description=""):
    from familysearch.config import url_prefixes
    host = host or urlsplit(url).hostname
    return {"id": hashlib.sha256(url.encode()).hexdigest()[:32], "url": url, "host": host,
            "title": title, "description": description, "headings": "", "body": body,
            "category": category, "language": "en", "boost": 0, "prefixes": url_prefixes(url)}


def seed(fake_meili, *docs):
    idx = fake_meili.indexes.setdefault(WEB, {})
    for d in docs:
        idx[d["id"]] = d


SEED = [
    wdoc("https://www.nasa.gov/moon", "The Moon", "The moon orbits Earth.", category="space"),
    wdoc("https://spaceplace.nasa.gov/moon-phases/", "Moon phases", "Why the moon changes shape.", category="space"),
    wdoc("https://kids.nationalgeographic.com/animals/", "Animals", "Animals of the world, moon bears too.",
         category="animals-science"),
    wdoc("https://www.merriam-webster.com/dictionary/moon", "Moon definition", "moon noun", category="reference"),
    wdoc("https://www.wonderopolis.org/wonder/why-is-the-sky-blue", "Why is the sky blue?", "light", category="curiosity"),
    wdoc("https://www.mathsisfun.com/geometry/circle.html", "Circle", "a moon is round like a circle", category="math"),
    wdoc("https://www.timeforkids.com/g56/moon-mission/", "Moon mission", "news about the moon", category="news"),
    wdoc("https://www.ducksters.com/animals/moon_bear.php", "Moon bear", "moon bear facts", category="reference"),
    wdoc("https://www.ducksters.com/history/moon_landing.php", "Moon landing", "apollo moon", category="reference"),
    wdoc("https://www.ducksters.com/games/moon_game.php", "Moon game", "a moon game", category="reference"),
    wdoc("https://www.merriam-webster.com/games/moon-quiz", "Moon quiz", "moon word game", category="reference"),
    # Stale / hostile documents that must never surface:
    wdoc("https://evil.example.com/moon", "Evil moon", "moon", host="evil.example.com"),
    wdoc("https://nasa.gov/moon-apex", "Apex domain moon", "moon", host="nasa.gov"),
    wdoc("http://www.nasa.gov/insecure-moon", "Insecure moon", "moon", host="www.nasa.gov"),
    wdoc("https://evil.example.com/disguised", "Disguised moon", "moon", host="www.nasa.gov"),
    wdoc("javascript:alert(1)", "JS moon", "moon", host="www.nasa.gov"),
    wdoc("https://www.nasa.gov:8443/port-moon", "Port moon", "moon", host="www.nasa.gov"),
]


def approved_hosts(core):
    return core.cfg.approved_hosts()


def test_every_result_url_exactly_matches_an_approved_host(child, core, fake_meili):
    seed(fake_meili, *SEED)
    allowed = approved_hosts(core)
    for t in ["all", "web"]:
        for page in (1, 2):
            data = child.get(f"/api/search?q=moon&type={t}&page={page}").json()
            for r in data["results"] + data["library"]:
                u = urlsplit(r["url"])
                assert u.scheme == "https" and u.hostname in allowed and u.port is None, r["url"]
    titles = {r["title"].replace(PRE, "").replace(POST, "")
              for r in child.get("/api/search?q=moon&type=web").json()["results"]}
    assert "The Moon" in titles
    assert not titles & {"Evil moon", "Apex domain moon", "Insecure moon", "Disguised moon",
                         "JS moon", "Port moon"}


def test_removing_a_host_hides_results_before_next_page_load(parent, child, fake_meili):
    seed(fake_meili, *SEED)
    fake_meili.ignore_deletes = True  # prove hiding does not depend on the index delete
    assert any("nasa.gov" in r["url"] for r in child.get("/api/search?q=moon&type=web").json()["results"])
    assert parent.post("/api/parent/sites", json={"action": "remove", "host": "www.nasa.gov"}).status_code == 200
    urls = [r["url"] for r in child.get("/api/search?q=moon&type=web").json()["results"]]
    assert not any(urlsplit(u).hostname == "www.nasa.gov" for u in urls)
    # and with deletes working, the documents are gone from the index too
    fake_meili.ignore_deletes = False
    assert parent.post("/api/parent/sites", json={"action": "disable", "host": "spaceplace.nasa.gov"}).status_code == 200
    assert not any(d["host"] == "spaceplace.nasa.gov" for d in fake_meili.indexes[WEB].values())
    assert not any("spaceplace" in r["url"] for r in child.get("/api/search?q=moon&type=web").json()["results"])


def test_blocked_terms_produce_the_safe_message(child, fake_meili, parent):
    seed(fake_meili, wdoc("https://www.nasa.gov/x", "naked mole rat", "naked"))
    for q in ["naked", "NAKED mole", "x x x", "nude", "NUDE!!", "por n"]:
        d = child.get("/api/search", params={"q": q}).json()
        assert d["blocked"] is True and d["results"] == [] and d["photos"] == [], q
    assert child.get("/api/search?q=snaked river").json()["blocked"] is False
    # parent edits take effect immediately
    parent.post("/api/parent/filters", json={"blocked": ["volcano"], "synonyms": {}})
    assert child.get("/api/search?q=volcano facts").json()["blocked"] is True
    assert child.get("/api/search?q=naked").json()["blocked"] is False


def test_suggestions_respect_blocks_and_approval(child, fake_meili):
    seed(fake_meili, *SEED)
    s = child.get("/api/suggest?q=moo").json()["suggestions"]
    assert s and not {"Evil moon", "Apex domain moon"} & set(s)
    assert child.get("/api/suggest?q=porn").json()["suggestions"] == []


def test_empty_results_never_fall_back_to_the_wider_web(child, fake_meili, core):
    seed(fake_meili, *SEED)
    d = child.get("/api/search?q=zebrafinchquasar").json()
    assert d["results"] == [] and d["library"] == [] and d["photos"] == []
    assert set(d) <= {"q", "type", "blocked", "page", "tile", "results", "total_pages", "photos", "library"}
    # every outbound call the app made during the request went to Meilisearch only
    assert all(p.startswith(("/indexes", "/health", "/tasks")) for _, p in fake_meili.requests)


def test_crawled_script_text_is_returned_as_inert_text(child, fake_meili):
    seed(fake_meili, wdoc("https://www.nasa.gov/xss", "<script>alert('t')</script> Moon",
                          "<img src=x onerror=alert(1)> moon facts", category="space"))
    r = child.get("/api/search?q=moon&type=web")
    assert r.headers["content-type"].startswith("application/json")
    hit = r.json()["results"][0]
    assert "<script>" in hit["title"]  # inert text; the UI renders it with textContent
    assert hit["url"] == "https://www.nasa.gov/xss"


def test_snippets_are_short_and_never_the_full_body(child, fake_meili):
    body = "moon " + ("filler words " * 3000)
    seed(fake_meili, wdoc("https://www.nasa.gov/long", "Long", body, category="space"))
    hit = child.get("/api/search?q=moon&type=web").json()["results"][0]
    assert len(hit["snippet"]) <= 330
    assert "body" not in hit
    assert hit["snippet"].count(PRE) == hit["snippet"].count(POST)


def test_each_tile_performs_its_configured_action(child, fake_meili, core, parent):
    seed(fake_meili, *SEED)
    cfg = child.get("/api/config").json()
    tiles = {t["id"]: t for t in cfg["tiles"]}
    assert set(tiles) == {"homework", "science", "space", "animals", "history", "math", "words",
                          "news", "ballet", "curious", "library", "photos"}
    hosts = core.cfg.hosts()

    def hosts_for(tile):
        d = child.get(f"/api/search?tile={tile}&q=").json()
        return {urlsplit(r["url"]).hostname for r in d["results"]}, d

    got, _ = hosts_for("space")
    assert got and all(hosts[h].category == "space" for h in got)
    def urls_for(tile, q=""):
        return {r["url"] for r in child.get(f"/api/search?tile={tile}&q={q}").json()["results"]}
    # Animals: just the animal sections of Nat Geo Kids and Ducksters
    assert urls_for("animals") == {"https://kids.nationalgeographic.com/animals/",
                                   "https://www.ducksters.com/animals/moon_bear.php"}
    # Words: the dictionary section of Merriam-Webster, not its games
    assert urls_for("words") == {"https://www.merriam-webster.com/dictionary/moon"}
    # History: history-category sites plus only Ducksters' history section
    assert "https://www.ducksters.com/history/moon_landing.php" in urls_for("history", "moon")
    assert not any("/games/" in u for u in urls_for("history", "moon") | urls_for("animals", "moon"))
    got, _ = hosts_for("math")
    assert got == {"www.mathsisfun.com"}
    got, _ = hosts_for("news")
    assert got == {"www.timeforkids.com"}
    got, _ = hosts_for("homework")
    assert all(hosts[h].category in ("homework", "reference", "writing") for h in got)
    # Curious opens a random wonderopolis page
    r = child.get("/api/curious?tile=curious").json()
    assert urlsplit(r["url"]).hostname == "www.wonderopolis.org"
    assert urlsplit(child.get("/api/curious").json()["url"]).hostname == "www.wonderopolis.org"
    # Photos tile switches to photo results
    assert child.get("/api/search?tile=photos").json()["type"] == "photos"
    # Library tile shows parent library items
    parent.post("/api/parent/library", json={"action": "add_note", "title": "Spelling list",
                                             "note": "moon, star, comet"})
    d = child.get("/api/search?tile=library&q=").json()
    assert [x["title"] for x in d["library"]] == ["Spelling list"]
    # a hidden tile does nothing
    t = core.cfg.tiles()
    t["tiles"][0]["hidden"] = True
    core.cfg.save_tiles(t)
    assert child.get("/api/search?tile=homework&q=moon").json()["results"] == []
    # Search-within-tile keeps the scope
    got, _ = hosts_for("space")
    d = child.get("/api/search?tile=space&q=moon").json()
    assert {urlsplit(r["url"]).hostname for r in d["results"]} <= {"www.nasa.gov", "spaceplace.nasa.gov"}


def test_library_items(parent, child, core):
    r = parent.post("/api/parent/library", json={"action": "add_url", "url": "http://insecure.example/",
                                                 "title": "x"})
    assert r.status_code == 400
    r = parent.post("/api/parent/library", json={"action": "add_url",
                                                 "url": "https://www.rhpl.org/kids/reading-list",
                                                 "title": "Summer reading", "note": "moon books"})
    assert r.status_code == 200
    d = child.get("/api/search?q=summer").json()
    assert d["library"][0]["url"] == "https://www.rhpl.org/kids/reading-list"
    # PDFs: type validated, stored outside web root, served sandboxed
    bad = parent.tc.post("/api/parent/library/pdf", headers=parent.headers(
        {"Origin": "https://search.thestrzs.net", "X-CSRF-Token": parent.csrf}),
        data={"title": "fake"}, files={"file": ("x.pdf", b"<html>not a pdf</html>", "application/pdf")})
    assert bad.status_code == 400
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    ok = parent.tc.post("/api/parent/library/pdf", headers=parent.headers(
        {"Origin": "https://search.thestrzs.net", "X-CSRF-Token": parent.csrf}),
        data={"title": "Moon worksheet", "note": "phases"},
        files={"file": ("w.pdf", pdf, "application/pdf")})
    assert ok.status_code == 200, ok.text
    lid = ok.json()["id"]
    f = child.get(f"/api/library/{lid}/file")
    assert f.status_code == 200 and f.content == pdf
    assert f.headers["content-security-policy"].startswith("sandbox")
    assert f.headers["content-type"] == "application/pdf"
    stored = list(core.s.library_dir.glob("*.pdf"))
    assert len(stored) == 1 and core.s.frontend_dir not in stored[0].parents
    assert child.get("/static/../../" + stored[0].name).status_code == 404
    # parent removes it -> gone
    parent.post("/api/parent/library", json={"action": "remove", "id": lid})
    assert child.get(f"/api/library/{lid}/file").status_code == 404
    assert not stored[0].exists()


def test_ask_a_grownup_saves_only_on_tap(child, parent, core):
    child.get("/api/search?q=dinosaur eggs")
    assert parent.get("/api/parent/state").json()["requests"] == []
    assert child.post("/api/requests", json={"kind": "query", "text": "dinosaur eggs"}).status_code == 200
    reqs = parent.get("/api/parent/state").json()["requests"]
    assert [r["text"] for r in reqs] == ["dinosaur eggs"]
    assert parent.post("/api/parent/requests", json={"action": "approve", "id": reqs[0]["id"]}).status_code == 200


def test_search_history_off_by_default_and_parent_toggle(child, parent, core):
    child.get("/api/search?q=volcanoes")
    with core.db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM search_history").fetchone()[0] == 0
    parent.post("/api/parent/settings", json={"store_history": True})
    child.get("/api/search?q=volcanoes")
    assert [r["q"] for r in parent.get("/api/parent/history").json()["rows"]] == ["volcanoes"]
    parent.post("/api/parent/settings", json={"store_history": False})
    with core.db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM search_history").fetchone()[0] == 0


def test_parent_changes_are_audited_and_backed_up(parent, core):
    r = parent.post("/api/parent/sites", json={"action": "add", "host": "www.example.org",
                                               "category": "reference", "boost": 1})
    assert r.status_code == 200
    assert parent.post("/api/parent/sites", json={"action": "add", "host": "*.example.org",
                                                  "category": "x"}).status_code == 400
    assert parent.post("/api/parent/sites", json={"action": "add", "host": "10.0.0.5",
                                                  "category": "x"}).status_code == 400
    hist = list((core.s.config_dir / ".history").glob("allowlist.*.yaml"))
    assert hist, "a dated backup of allowlist.yaml is kept"
    doc = yaml.safe_load((core.s.config_dir / "allowlist.yaml").read_text())
    assert doc["hosts"]["www.example.org"]["category"] == "reference"
    assert (core.s.spool_dir / "crawl-now").exists()  # new site gets crawled
    rows = parent.get("/api/parent/audit?kind=parent").json()["rows"]
    assert rows[0]["action"] == "site added" and rows[0]["actor"] == "david@example.com"
    fl = {h["host"]: h["confirmed"] for h in parent.get("/api/parent/state").json()["familylink"]}
    assert fl["www.example.org"] is False and "search.thestrzs.net" in fl
    assert "strzfamily.cloudflareaccess.com" in fl
    parent.post("/api/parent/familylink", json={"host": "www.example.org", "confirmed": True})
    fl = {h["host"]: h["confirmed"] for h in parent.get("/api/parent/state").json()["familylink"]}
    assert fl["www.example.org"] is True


def test_tiles_editable_without_code_changes(parent, child):
    st = parent.get("/api/parent/state").json()
    tiles = st["tiles"]["tiles"]
    tiles.reverse()
    tiles[0]["hidden"] = True
    tiles.append({"id": "desmos", "label": "Graphing", "icon": "calculator", "action": "open_host",
                  "hosts": ["www.desmos.com"]})
    assert parent.post("/api/parent/tiles", json={"tiles": tiles}).status_code == 200
    ids = [t["id"] for t in child.get("/api/config").json()["tiles"]]
    assert ids[0] == "library" and "photos" not in ids and ids[-1] == "desmos"
    t = [t for t in child.get("/api/config").json()["tiles"] if t["id"] == "desmos"][0]
    assert t["url"] == "https://www.desmos.com/"
    bad = tiles + [{"id": "x", "label": "Bad", "icon": "star", "action": "open_host", "hosts": ["http://x"]}]
    assert parent.post("/api/parent/tiles", json={"tiles": bad}).status_code == 400


def test_index_rebuilds_from_config_and_sqlite(parent, child, core, fake_meili, tmp_path):
    from familysearch.library import LibraryService
    parent.post("/api/parent/library", json={"action": "add_note", "title": "Chore chart",
                                             "note": "feed the cat"})
    fake_meili.indexes.clear()  # lose the whole index
    assert child.get("/api/search?q=chore").json()["library"] == []
    core.meili.ensure_indexes({})  # what `familysearch.cli reindex-local` does first
    LibraryService(core.db, core.meili, core.s.library_dir).reindex()
    core.photos.reindex()
    assert child.get("/api/search?q=chore").json()["library"][0]["title"].replace(PRE, "").replace(POST, "") == "Chore chart"
    assert LIBRARY in fake_meili.indexes and PHOTOS in fake_meili.indexes


def test_search_unavailable_is_a_calm_503(child, fake_meili):
    fake_meili.down = True
    r = child.get("/api/search?q=moon")
    assert r.status_code == 503 and r.json()["unavailable"] is True


def test_secrets_never_in_responses(app, keys, fake_meili):
    seed(fake_meili, *SEED)
    c = Client(app, keys, CHILD)
    for p in ["/", "/api/config", "/api/me", "/api/search?q=moon", "/theme.css", "/manifest.webmanifest",
              "/static/app.js"]:
        c.get(p)
    blob = b"\n".join(c.bodies)
    for secret in [b"meili-test-key", b"csrf-test-secret", b"immich-secret-key", b"192.168.0.101", b"2283",
                   b"7700"]:
        assert secret not in blob, secret


def test_tile_sections_filter_down_and_stay_default_deny(parent, child, core, fake_meili):
    from familysearch.config import ConfigError, section_key, url_prefixes
    seed(fake_meili, *SEED)
    assert url_prefixes("https://www.ducksters.com/history/us/Civil_War.php") == [
        "www.ducksters.com/", "www.ducksters.com/history/", "www.ducksters.com/history/us/",
        "www.ducksters.com/history/us/civil_war.php/"]
    assert section_key("www.ducksters.com/history") == "www.ducksters.com/history/"
    for bad in ["http://www.ducksters.com/history/", "https://x.org/a?b=1", "https://*.x.org/"]:
        try:
            section_key(bad)
            raise AssertionError(bad)
        except ConfigError:
            pass
    tiles = parent.get("/api/parent/state").json()["tiles"]["tiles"]
    tiles.append({"id": "mix", "label": "Mix", "icon": "star", "action": "filter",
                  "sections": ["https://www.ducksters.com/history/", "https://evil.example.com/"]})
    assert parent.post("/api/parent/tiles", json={"tiles": tiles}).status_code == 200
    # a section on an unapproved host contributes nothing (default deny)
    urls = {r["url"] for r in child.get("/api/search?tile=mix&q=moon").json()["results"]}
    assert urls == {"https://www.ducksters.com/history/moon_landing.php"}
    # "history" must not match a sibling like /historyx/
    seed(fake_meili, wdoc("https://www.ducksters.com/historyx/moon.php", "Not history", "moon"))
    urls = {r["url"] for r in child.get("/api/search?tile=mix&q=moon").json()["results"]}
    assert urls == {"https://www.ducksters.com/history/moon_landing.php"}
    # disabling the site hides its sections immediately
    parent.post("/api/parent/sites", json={"action": "disable", "host": "www.ducksters.com"})
    assert child.get("/api/search?tile=mix&q=moon").json()["results"] == []
    # parent page shows how many pages each tile reaches, sections as links
    st = parent.get("/api/parent/state").json()
    assert st["tile_counts"]["mix"] == 0 and st["tile_counts"]["words"] == 1
    mix = [t for t in st["tiles"]["tiles"] if t["id"] == "mix"][0]
    assert mix["sections"][0] == "https://www.ducksters.com/history/"
    import yaml
    saved = yaml.safe_load((core.s.config_dir / "tiles.yaml").read_text())
    assert [t for t in saved["tiles"] if t["id"] == "mix"][0]["sections"][0] == "https://www.ducksters.com/history/"
