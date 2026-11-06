"""Acceptance: Crawler. Runs the real Crawler + Fetcher against a fake web:
only the final socket exchange and DNS are simulated."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from crawler import Crawler, Store
from extract import extract, normalize_url, skip_reason
from familysearch.config import ConfigStore, load_settings
from familysearch.meili import WEB, Meili
from safefetch import Fetcher, FetchError, Response, ip_allowed

PUBLIC = "93.184.216.34"


def html(title, body="", links=(), extra_head=""):
    a = "".join(f'<a href="{h}">link</a>' for h in links)
    return (f"<html lang='en'><head><title>{title}</title>{extra_head}</head>"
            f"<body><h1>{title}</h1><p>{body}</p>{a}<script>var secret='do-not-index';</script>"
            f"</body></html>").encode()


class FakeWeb(Fetcher):
    def __init__(self, pages, dns=None):
        super().__init__(resolve=self._resolve)
        self.pages = pages          # (host, path) -> (status, headers, body)
        self.dns = dns or {}
        self.hits: list[str] = []

    def _resolve(self, host):
        if host in self.dns:
            return self.dns[host]
        return [PUBLIC]

    def _exchange(self, host, ip, path, deadline, max_bytes, accept, sink=None):
        assert self.ip_ok(ip), "connected to a non-public address"
        self.hits.append(f"https://{host}{path}")
        status, headers, body = self.pages.get((host, path), (404, {}, b""))
        headers = {k.lower(): v for k, v in headers.items()}
        if status in (301, 302, 303, 307, 308) or status >= 400:
            return Response(url="", status=status, headers=headers)
        if accept.startswith("text/html") and headers.get("content-type", "").split(";")[0] not in (
                "text/html", "application/xhtml+xml"):
            return Response(url="", status=status, headers=headers)
        if len(body) > max_bytes:
            raise FetchError("too_large")
        return Response(url="", status=status, headers=headers, body=body)


H = {"content-type": "text/html; charset=utf-8"}
T = {"content-type": "text/plain"}


@pytest.fixture
def cfgstore(env):
    return ConfigStore(load_settings())


def write_allowlist(cfgstore, hosts: dict):
    import yaml
    (cfgstore.settings.config_dir / "allowlist.yaml").write_text(yaml.safe_dump({"hosts": hosts}))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def run(cfgstore, web, fake_meili, tmp_path, **kw):
    clock = Clock()
    store = Store(tmp_path / "crawl.db")
    meili = Meili("http://127.0.0.1:7700", "k", transport=fake_meili.transport())
    c = Crawler(cfgstore, store, meili, web, sleep=clock.sleep, clock=clock, **kw)
    status = c.run(force_all=True)
    return c, status, store


def indexed_urls(fake_meili):
    return {d["url"] for d in fake_meili.indexes.get(WEB, {}).values()}


def test_redirect_to_unapproved_host_is_not_indexed(cfgstore, fake_meili, tmp_path):
    write_allowlist(cfgstore, {"a.example.org": {"category": "reference"}})
    web = FakeWeb({
        ("a.example.org", "/robots.txt"): (200, T, b"User-agent: *\nAllow: /\n"),
        ("a.example.org", "/"): (200, H, html("Home", "welcome", ["/ok", "/away"])),
        ("a.example.org", "/ok"): (200, H, html("Okay page", "fine")),
        ("a.example.org", "/away"): (302, {"Location": "https://evil.example.net/landing"}, b""),
        ("evil.example.net", "/landing"): (200, H, html("Evil", "bad")),
    })
    c, status, store = run(cfgstore, web, fake_meili, tmp_path)
    urls = indexed_urls(fake_meili)
    assert "https://a.example.org/ok" in urls
    assert not any("evil" in u for u in urls) and "https://a.example.org/away" not in urls
    assert not any("evil.example.net" in h for h in web.hits), "the unapproved hop was never fetched"


def test_redirect_to_http_is_refused(cfgstore, fake_meili, tmp_path):
    write_allowlist(cfgstore, {"a.example.org": {"category": "reference"}})
    web = FakeWeb({
        ("a.example.org", "/robots.txt"): (200, T, b""),
        ("a.example.org", "/"): (301, {"Location": "http://a.example.org/plain"}, b""),
    })
    run(cfgstore, web, fake_meili, tmp_path)
    assert indexed_urls(fake_meili) == set()


def test_hostname_resolving_to_private_address_is_blocked(cfgstore, fake_meili, tmp_path):
    write_allowlist(cfgstore, {"rebind.example.org": {"category": "reference"},
                               "mixed.example.org": {"category": "reference"}})
    web = FakeWeb({("rebind.example.org", "/robots.txt"): (200, T, b""),
                   ("rebind.example.org", "/"): (200, H, html("Should not be fetched"))},
                  dns={"rebind.example.org": ["192.168.0.126"],
                       "mixed.example.org": [PUBLIC, "169.254.169.254"]})
    c, status, store = run(cfgstore, web, fake_meili, tmp_path)
    assert web.hits == []
    assert status["hosts"]["rebind.example.org"]["status"] == "needs_review"
    assert status["hosts"]["mixed.example.org"]["status"] == "needs_review"
    for ip in ["10.1.2.3", "127.0.0.1", "169.254.169.254", "100.64.1.1", "172.16.5.4", "::1",
               "fe80::1", "fd00::1", "::ffff:192.168.0.1", "0.0.0.0", "224.0.0.1"]:
        assert not ip_allowed(ip), ip
    assert ip_allowed(PUBLIC) and ip_allowed("2606:4700::1111")


def test_http_links_are_not_followed(cfgstore, fake_meili, tmp_path):
    write_allowlist(cfgstore, {"a.example.org": {"category": "reference"}})
    web = FakeWeb({
        ("a.example.org", "/robots.txt"): (200, T, b""),
        ("a.example.org", "/"): (200, H, html("Home", "x", ["http://a.example.org/insecure",
                                                           "https://a.example.org/secure"])),
        ("a.example.org", "/secure"): (200, H, html("Secure", "y")),
        ("a.example.org", "/insecure"): (200, H, html("Insecure", "z")),
    })
    run(cfgstore, web, fake_meili, tmp_path)
    assert "https://a.example.org/insecure" not in web.hits
    assert "https://a.example.org/secure" in indexed_urls(fake_meili)


def test_robots_rules_are_followed(cfgstore, fake_meili, tmp_path):
    write_allowlist(cfgstore, {"a.example.org": {"category": "reference"}})
    web = FakeWeb({
        ("a.example.org", "/robots.txt"): (200, T, b"User-agent: *\nDisallow: /private\n"),
        ("a.example.org", "/"): (200, H, html("Home", "x", ["/private/page", "/public"])),
        ("a.example.org", "/public"): (200, H, html("Public", "y")),
        ("a.example.org", "/private/page"): (200, H, html("Private", "z")),
    })
    run(cfgstore, web, fake_meili, tmp_path)
    assert "https://a.example.org/private/page" not in web.hits
    assert "https://a.example.org/public" in indexed_urls(fake_meili)


def test_site_that_blocks_crawling_is_marked_blocked_not_bypassed(cfgstore, fake_meili, tmp_path):
    write_allowlist(cfgstore, {"a.example.org": {"category": "reference"}})
    web = FakeWeb({
        ("a.example.org", "/robots.txt"): (200, T, b"User-agent: *\nDisallow: /\n"),
        ("a.example.org", "/"): (200, H, html("Home")),
    })
    c, status, _ = run(cfgstore, web, fake_meili, tmp_path)
    assert web.hits == ["https://a.example.org/robots.txt"]
    assert status["hosts"]["a.example.org"]["status"] == "blocked"


def test_challenged_site_is_reported_not_bypassed(cfgstore, fake_meili, tmp_path):
    write_allowlist(cfgstore, {"a.example.org": {"category": "reference"}})
    web = FakeWeb({
        ("a.example.org", "/robots.txt"): (200, T, b""),
        ("a.example.org", "/"): (403, {"cf-mitigated": "challenge", "server": "cloudflare"}, b""),
        ("a.example.org", "/sitemap.xml"): (200, {"content-type": "application/xml"},
                                             b"<urlset><url><loc>https://a.example.org/p1</loc></url></urlset>"),
        ("a.example.org", "/p1"): (200, H, html("P1")),
    })
    c, status, _ = run(cfgstore, web, fake_meili, tmp_path)
    assert status["hosts"]["a.example.org"]["status"] == "challenged"
    assert web.hits.count("https://a.example.org/") == 1  # no retries, no tricks
    assert "challenged" in json.dumps(status) and status["last_run"]["problems"] == ["a.example.org"]


def test_per_host_limits_and_delays_are_enforced(cfgstore, fake_meili, tmp_path):
    write_allowlist(cfgstore, {"a.example.org": {"category": "reference", "page_cap": 3},
                               "b.example.org": {"category": "reference"}})
    pages = {}
    for host in ("a.example.org", "b.example.org"):
        pages[(host, "/robots.txt")] = (200, T, b"")
        pages[(host, "/")] = (200, H, html("Home", "x", [f"/p{i}" for i in range(10)]))
        for i in range(10):
            pages[(host, f"/p{i}")] = (200, H, html(f"Page {i}", "words", [f"/p{i}/deeper"]))
    web = FakeWeb(pages)
    c, status, store = run(cfgstore, web, fake_meili, tmp_path)
    assert store.page_count("a.example.org") == 3
    assert store.page_count("b.example.org") > 3
    for host in ("a.example.org", "b.example.org"):
        times = [t for h, t in c.fetch_log if h == host]
        gaps = [b - a for a, b in zip(times, times[1:])]
        assert gaps and min(gaps) >= 5.0, (host, min(gaps))


def test_crawl_delay_from_robots_is_honoured(cfgstore, fake_meili, tmp_path):
    write_allowlist(cfgstore, {"a.example.org": {"category": "reference"}})
    web = FakeWeb({
        ("a.example.org", "/robots.txt"): (200, T, b"User-agent: *\nCrawl-delay: 12\n"),
        ("a.example.org", "/"): (200, H, html("Home", "x", ["/p"])),
        ("a.example.org", "/p"): (200, H, html("P", "y")),
    })
    c, _, _ = run(cfgstore, web, fake_meili, tmp_path)
    times = [t for _, t in c.fetch_log]
    assert min(b - a for a, b in zip(times, times[1:])) >= 12


def test_depth_limit_skip_rules_tracking_and_canonical(cfgstore, fake_meili, tmp_path):
    write_allowlist(cfgstore, {"a.example.org": {"category": "reference"}})
    chain = {("a.example.org", "/robots.txt"): (200, T, b"")}
    chain[("a.example.org", "/")] = (200, H, html("Home", "x", [
        "/d1", "/login", "/account/settings", "/tag/space", "/forum/thread", "/cart", "/print/1",
        "/article?utm_source=x&id=7&fbclid=y#top", "/file.pdf"]))
    for d in range(1, 7):
        chain[("a.example.org", f"/d{d}")] = (200, H, html(f"Depth {d}", "x", [f"/d{d + 1}"]))
    chain[("a.example.org", "/article?id=7")] = (200, H, html(
        "Article", "y", extra_head='<link rel="canonical" href="https://a.example.org/articles/7">'))
    web = FakeWeb(chain)
    run(cfgstore, web, fake_meili, tmp_path)
    urls = indexed_urls(fake_meili)
    assert "https://a.example.org/d4" in urls and "https://a.example.org/d5" not in urls
    for skipped in ["/login", "/account/settings", "/tag/space", "/forum/thread", "/cart", "/print/1",
                    "/file.pdf"]:
        assert f"https://a.example.org{skipped}" not in web.hits, skipped
    assert "https://a.example.org/article?id=7" in web.hits          # tracking params stripped
    assert "https://a.example.org/articles/7" in urls                 # stored under canonical URL
    assert not any("utm_" in u or "fbclid" in u for u in web.hits)


def test_stored_text_is_text_not_markup(cfgstore, fake_meili, tmp_path):
    write_allowlist(cfgstore, {"a.example.org": {"category": "reference"}})
    web = FakeWeb({
        ("a.example.org", "/robots.txt"): (200, T, b""),
        ("a.example.org", "/"): (200, H, b"<html><head><title>&lt;script&gt;alert(1)&lt;/script&gt;</title></head>"
                                          b"<body><p>Hello &lt;b&gt;there&lt;/b&gt;</p>"
                                          b"<script>steal()</script><style>x{}</style></body></html>"),
    })
    run(cfgstore, web, fake_meili, tmp_path)
    doc = list(fake_meili.indexes[WEB].values())[0]
    assert doc["title"] == "<script>alert(1)</script>"   # literal text, escaped later by the UI
    assert "steal()" not in doc["body"] and "x{}" not in doc["body"]
    assert len(doc["body"]) <= 20000


def test_non_html_and_oversized_responses_are_skipped(cfgstore, fake_meili, tmp_path):
    write_allowlist(cfgstore, {"a.example.org": {"category": "reference"}})
    web = FakeWeb({
        ("a.example.org", "/robots.txt"): (200, T, b""),
        ("a.example.org", "/"): (200, H, html("Home", "x", ["/json", "/big"])),
        ("a.example.org", "/json"): (200, {"content-type": "application/json"}, b"{}"),
        ("a.example.org", "/big"): (200, H, b"<html>" + b"a" * (5 * 1024 * 1024 + 10)),
    })
    run(cfgstore, web, fake_meili, tmp_path)
    assert indexed_urls(fake_meili) == {"https://a.example.org/"}


def test_removed_host_is_purged_on_next_run(cfgstore, fake_meili, tmp_path):
    write_allowlist(cfgstore, {"a.example.org": {"category": "reference"}})
    web = FakeWeb({("a.example.org", "/robots.txt"): (200, T, b""),
                   ("a.example.org", "/"): (200, H, html("Home", "x"))})
    run(cfgstore, web, fake_meili, tmp_path)
    assert indexed_urls(fake_meili)
    write_allowlist(cfgstore, {"b.example.org": {"category": "reference", "crawl": False}})
    c, status, store = run(cfgstore, web, fake_meili, tmp_path)
    assert indexed_urls(fake_meili) == set() and store.page_count("a.example.org") == 0


def test_exact_hostname_only(cfgstore, fake_meili, tmp_path):
    write_allowlist(cfgstore, {"example.org": {"category": "reference"}})
    web = FakeWeb({("example.org", "/robots.txt"): (200, T, b""),
                   ("example.org", "/"): (200, H, html("Home", "x", ["https://sub.example.org/x"])),
                   ("sub.example.org", "/x"): (200, H, html("Sub", "y"))})
    run(cfgstore, web, fake_meili, tmp_path)
    assert not any("sub.example.org" in h for h in web.hits)


def test_crawl_status_written_for_parent_page(cfgstore, fake_meili, tmp_path, parent):
    write_allowlist(cfgstore, {"a.example.org": {"category": "reference"}})
    web = FakeWeb({("a.example.org", "/robots.txt"): (200, T, b""),
                   ("a.example.org", "/"): (200, H, html("Home", "x"))})
    run(cfgstore, web, fake_meili, tmp_path)
    st = json.loads(Path(cfgstore.settings.crawl_status_path).read_text())
    h = st["hosts"]["a.example.org"]
    assert h["status"] == "ok" and h["pages"] == 1 and h["last_success"]
    rows = parent.get("/api/parent/audit?kind=crawl").json()["rows"]
    assert rows and rows[0]["action"] == "crawl finished"


def test_reindex_from_page_store(cfgstore, fake_meili, tmp_path):
    write_allowlist(cfgstore, {"a.example.org": {"category": "reference"}})
    web = FakeWeb({("a.example.org", "/robots.txt"): (200, T, b""),
                   ("a.example.org", "/"): (200, H, html("Home", "x", ["/p"])),
                   ("a.example.org", "/p"): (200, H, html("P", "y"))})
    c, _, store = run(cfgstore, web, fake_meili, tmp_path)
    fake_meili.indexes.clear()
    assert c.reindex() == 2
    assert len(indexed_urls(fake_meili)) == 2


def test_url_helpers():
    assert normalize_url("HTTPS://A.Example.org:443//x//y?b=2&utm_campaign=z&a=1#frag") == \
        "https://a.example.org/x/y?a=1&b=2"
    assert normalize_url("http://a.example.org/") is None
    assert normalize_url("https://user:pw@a.example.org/") is None
    assert normalize_url("https://a.example.org:8443/") is None
    assert skip_reason("https://a.example.org/how-animals-feed") is None
    assert skip_reason("https://a.example.org/accounting-for-kids") is None
    assert skip_reason("https://a.example.org/sign-in") == "skipped page type"
    p = extract("<html><head><meta name='robots' content='noindex,nofollow'></head><body>x</body></html>",
                "https://a.example.org/")
    assert p.noindex and p.nofollow


def test_wikitext_stripping():
    from wikidump import first_sentences, strip_wikitext
    text = ("{{Infobox planet|name=Moon|{{nested|x}}}}\n'''The Moon''' is Earth's only [[natural satellite]]. "
            "It is about [[Distance|384,400 km]] away.<ref>{{cite web|url=x}}</ref>\n"
            "[[File:Moon.jpg|thumb|The [[Moon]] at night]]\n== Phases ==\n* New moon\n"
            "{| class=wikitable\n|-\n| a || b\n|}\n[https://nasa.gov NASA] says hi.\n"
            "== References ==\n[[Category:Moons]]\n<!-- hidden -->")
    body, heads = strip_wikitext(text)
    assert body.startswith("The Moon is Earth's only natural satellite. It is about 384,400 km away.")
    assert "Infobox" not in body and "cite" not in body and "File:" not in body and "Category" not in body
    assert "wikitable" not in body and "hidden" not in body and "NASA says hi." in body
    assert heads == ["Phases"]
    assert first_sentences("A. " * 400).endswith(".")


def test_indexed_pages_carry_section_prefixes(cfgstore, fake_meili, tmp_path):
    write_allowlist(cfgstore, {"a.example.org": {"category": "reference"}})
    web = FakeWeb({("a.example.org", "/robots.txt"): (200, T, b""),
                   ("a.example.org", "/"): (200, H, html("Home", "x", ["/animals/lion"])),
                   ("a.example.org", "/animals/lion"): (200, H, html("Lion", "roar"))})
    run(cfgstore, web, fake_meili, tmp_path)
    doc = [d for d in fake_meili.indexes[WEB].values() if d["url"].endswith("/lion")][0]
    assert "a.example.org/animals/" in doc["prefixes"]
