#!/usr/bin/env python3
"""Family Search crawler. Runs as its own unprivileged user from
family-crawler.timer (nightly) or family-crawler.path (parent pressed "Crawl").

  python crawler.py                 # nightly run: hosts that are due
  python crawler.py --host H [...]  # just these hosts, now
  python crawler.py --all           # every crawlable host, now
  python crawler.py --reindex       # push the page store into Meilisearch (rebuild)

Rules (see CLAUDE.md): allowlist.yaml re-read at the start of every run; exact
hostnames; HTTPS only; every redirect validated; private addresses refused;
robots.txt obeyed (a site that blocks us is marked `blocked`, never bypassed);
challenges are reported, never solved; one request at a time with >= 5 s per
host; sitemaps first, then internal links to depth 4; per-host page cap; HTML
only, 5 MB / 20 s; account/checkout/comment/forum/tag/print pages skipped;
tracking parameters stripped; canonical URLs used. We keep searchable text,
never mirrored pages.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
import time
import zlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib import robotparser
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "backend"))
sys.path.insert(0, str(HERE))

from familysearch.config import ConfigStore, Host, atomic_write, load_settings, url_prefixes  # noqa: E402
from familysearch.meili import WEB, Meili, MeiliError, quote_filter_value  # noqa: E402
from extract import extract, normalize_url, skip_reason  # noqa: E402
from safefetch import USER_AGENT, Fetcher, FetchError  # noqa: E402

MIN_DELAY = 5.0
MAX_DELAY = 60.0
MAX_DEPTH = 4
REFRESH_DAYS = 7
SITEMAP_MAX_FILES = 50
FLUSH_EVERY = 50
DEFAULT_MAX_MINUTES = 240
DOC_VERSION = 2  # bump when the indexed document shape changes, so refreshes re-push every page

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS pages (
  url TEXT PRIMARY KEY, id TEXT NOT NULL, host TEXT NOT NULL, title TEXT, description TEXT,
  headings TEXT, body TEXT, category TEXT, language TEXT, last_crawled TEXT, content_hash TEXT
);
CREATE INDEX IF NOT EXISTS pages_host ON pages(host);
CREATE TABLE IF NOT EXISTS frontier (
  url TEXT PRIMARY KEY, host TEXT NOT NULL, depth INTEGER NOT NULL, state TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS frontier_host ON frontier(host, state, depth);
CREATE TABLE IF NOT EXISTS hosts (
  host TEXT PRIMARY KEY, status TEXT, last_error TEXT, cycle_started TEXT, cycle_completed TEXT,
  last_run TEXT, last_success TEXT, found INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0,
  errors INTEGER DEFAULT 0, crawl_delay REAL, dump_sha1 TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def doc_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:32]


class Progress:
    """A running counter: a live line on a terminal, periodic lines in the journal."""

    def __init__(self, label: str):
        self.label = label
        self.tty = sys.stdout.isatty()
        self.counts = {"fetched": 0, "stored": 0, "skipped": 0, "errors": 0}
        self.last = 0.0
        self.host = ""

    def bump(self, key: str, host: str = ""):
        self.counts[key] += 1
        if host:
            self.host = host
        self.show()

    def show(self, force: bool = False):
        now = time.monotonic()
        if not force and now - self.last < (0.2 if self.tty else 30):
            return
        self.last = now
        c = self.counts
        line = (f"[{self.label}] fetched {c['fetched']} · stored {c['stored']} · "
                f"skipped {c['skipped']} · errors {c['errors']} · {self.host}")
        if self.tty:
            sys.stdout.write("\r" + line[:120].ljust(120))
        else:
            sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def done(self):
        self.show(force=True)
        if self.tty:
            sys.stdout.write("\n")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)

    def host_row(self, host: str) -> sqlite3.Row:
        self.c.execute("INSERT OR IGNORE INTO hosts(host, status) VALUES (?, 'new')", (host,))
        return self.c.execute("SELECT * FROM hosts WHERE host=?", (host,)).fetchone()

    def set_host(self, host: str, **kw):
        self.host_row(host)
        cols = ", ".join(f"{k}=?" for k in kw)
        self.c.execute(f"UPDATE hosts SET {cols} WHERE host=?", (*kw.values(), host))

    def bump_host(self, host: str, col: str, n: int = 1):
        self.c.execute(f"UPDATE hosts SET {col}=COALESCE({col},0)+? WHERE host=?", (n, host))

    def page_count(self, host: str) -> int:
        return self.c.execute("SELECT COUNT(*) FROM pages WHERE host=?", (host,)).fetchone()[0]

    def frontier_count(self, host: str) -> int:
        return self.c.execute("SELECT COUNT(*) FROM frontier WHERE host=?", (host,)).fetchone()[0]

    def queued(self, host: str) -> int:
        return self.c.execute("SELECT COUNT(*) FROM frontier WHERE host=? AND state='queued'",
                              (host,)).fetchone()[0]

    def enqueue(self, url: str, host: str, depth: int) -> bool:
        cur = self.c.execute("INSERT OR IGNORE INTO frontier(url, host, depth, state) VALUES (?,?,?,'queued')",
                             (url, host, depth))
        return cur.rowcount > 0

    def next_url(self, host: str):
        return self.c.execute("""SELECT url, depth FROM frontier WHERE host=? AND state='queued'
                                 ORDER BY depth, rowid LIMIT 1""", (host,)).fetchone()

    def mark(self, url: str, state: str = "done"):
        self.c.execute("UPDATE frontier SET state=? WHERE url=?", (state, url))

    def purge_host(self, host: str):
        self.c.execute("DELETE FROM pages WHERE host=?", (host,))
        self.c.execute("DELETE FROM frontier WHERE host=?", (host,))
        self.c.execute("DELETE FROM hosts WHERE host=?", (host,))


class Crawler:
    def __init__(self, cfg: ConfigStore, store: Store, meili: Meili | None, fetcher: Fetcher,
                 max_minutes: float = DEFAULT_MAX_MINUTES, min_delay: float = MIN_DELAY,
                 sleep=time.sleep, clock=time.monotonic):
        self.cfg = cfg
        self.store = store
        self.meili = meili
        self.fetcher = fetcher
        self.deadline = clock() + max_minutes * 60
        self.min_delay = min_delay
        self.sleep = sleep
        self.clock = clock
        self.hosts: dict[str, Host] = {}
        self.robots: dict[str, robotparser.RobotFileParser] = {}
        self.delay: dict[str, float] = {}
        self.next_at: dict[str, float] = {}
        self.stopped: set[str] = set()
        self.pending_docs: list[dict] = []
        self.pending_deletes: list[str] = []
        self.progress = Progress("crawl")
        self.fetch_log: list[tuple[str, float]] = []  # (host, time) — used by tests
        self.started = utcnow()

    # -- approval ------------------------------------------------------------
    def approved(self, host: str) -> bool:
        h = self.hosts.get(host)
        return bool(h and h.enabled)

    # -- pacing --------------------------------------------------------------
    def _fetch(self, host: str, url: str, **kw):
        wait = self.next_at.get(host, 0) - self.clock()
        if wait > 0:
            self.sleep(wait)
        self.fetch_log.append((host, self.clock()))
        try:
            return self.fetcher.get(url, self.approved, **kw)
        finally:
            self.next_at[host] = self.clock() + self.delay.get(host, self.min_delay)

    # -- run -----------------------------------------------------------------
    def run(self, only: list[str] | None = None, force_all: bool = False) -> dict:
        self.hosts = dict(self.cfg.hosts())  # fresh read at the start of every run
        self.purge_unapproved()
        if self.meili:
            try:
                self.meili.ensure_indexes(self.cfg.filters()["synonyms"])
            except MeiliError as e:
                print(f"warning: search index unavailable ({e}); pages will be stored and pushed later")
        due = self.due_hosts(only, force_all)
        print(f"crawl: {len(due)} host(s) due: {', '.join(due) or '-'}")
        crawl_hosts = [h for h in due if self.hosts[h].source == "crawl"]
        dump_hosts = [h for h in due if self.hosts[h].source == "wikimedia-dump"]
        for h in crawl_hosts:
            self.start_host(h)
        self.loop(crawl_hosts)
        self.flush()
        for h in crawl_hosts:
            self.finish_host(h)
        for h in dump_hosts:
            if self.clock() > self.deadline:
                break
            try:
                from wikidump import import_dump
                import_dump(self, h)
            except Exception as e:  # report, never retry around it
                self.store.set_host(h, status="needs_review", last_error=f"dump: {e}"[:300],
                                    last_run=utcnow())
        self.flush()
        self.progress.done()
        return self.write_status(final=True)

    def due_hosts(self, only: list[str] | None, force_all: bool) -> list[str]:
        now = time.time()
        today = datetime.now().weekday()
        out = []
        for name, h in sorted(self.hosts.items()):
            if not (h.enabled and h.crawl):
                continue
            if only is not None:
                if name in only:
                    out.append(name)
                continue
            if force_all:
                out.append(name)
                continue
            row = self.store.host_row(name)
            if self.store.queued(name):            # unfinished cycle: keep going, gradually
                out.append(name)
                continue
            done = parse_ts(row["cycle_completed"])
            day = h.schedule_day if h.schedule_day is not None else zlib.crc32(name.encode()) % 7
            if done is None or (now - done > (REFRESH_DAYS - 0.5) * 86400 and today == day) \
                    or now - done > (REFRESH_DAYS + 7) * 86400:
                out.append(name)
        return out

    def purge_unapproved(self):
        approved = {h for h, v in self.hosts.items() if v.enabled}
        rows = self.store.c.execute(
            "SELECT host FROM hosts UNION SELECT DISTINCT host FROM pages").fetchall()
        for r in rows:
            host = r[0]
            if host not in approved:
                print(f"purge: {host} is no longer approved — removing its pages")
                self.store.purge_host(host)
                if self.meili:
                    try:
                        self.meili.delete_by_filter(WEB, f"host = {quote_filter_value(host)}")
                    except MeiliError:
                        pass
        # Disabled-but-listed hosts: also make sure nothing of theirs is searchable.

    # -- per host --------------------------------------------------------------
    def start_host(self, host: str):
        h = self.hosts[host]
        row = self.store.host_row(host)
        self.store.set_host(host, last_run=utcnow())
        if not self.load_robots(host):
            return
        if self.store.queued(host):
            return  # continue an unfinished cycle
        # New cycle: drop the old frontier, seed from sitemaps + home page, and
        # re-queue known pages so refreshes catch changes and removals.
        self.store.c.execute("DELETE FROM frontier WHERE host=?", (host,))
        self.store.set_host(host, cycle_started=utcnow(), skipped=0, errors=0,
                            status="crawling" if row["status"] != "new" else "first crawl")
        home = f"https://{host}/"
        self.store.enqueue(home, host, 0)
        n = 1
        for url in self.sitemap_urls(host, h.page_cap):
            if self.store.enqueue(url, host, 0):
                n += 1
        for r in self.store.c.execute("SELECT url FROM pages WHERE host=?", (host,)).fetchall():
            self.store.enqueue(r["url"], host, 1)
        print(f"  {host}: seeded {n} url(s) from home page + sitemaps")

    def load_robots(self, host: str) -> bool:
        rp = robotparser.RobotFileParser()
        url = f"https://{host}/robots.txt"
        try:
            r = self._fetch(host, url, accept="text/plain,*/*;q=0.1", max_bytes=512 * 1024)
        except FetchError as e:
            if e.kind in ("blocked_ip", "unapproved", "not_https"):
                self.stop_host(host, "needs_review", f"robots.txt: {e}")
                return False
            self.stop_host(host, "error", f"robots.txt: {e}")
            return False
        if self._challenged(r):
            self.stop_host(host, "challenged", "bot challenge on robots.txt — not bypassed")
            return False
        if r.status in (401, 403):
            rp.disallow_all = True
        elif r.status >= 500 or r.status == 429:
            self.stop_host(host, "error", f"robots.txt returned {r.status}; will retry next run")
            return False
        elif r.status >= 400:
            rp.allow_all = True
        else:
            rp.parse(r.text().splitlines())
        self.robots[host] = rp
        if not rp.can_fetch(USER_AGENT, f"https://{host}/"):
            self.stop_host(host, "blocked", "robots.txt does not allow crawling — not bypassed")
            return False
        cd = rp.crawl_delay(USER_AGENT)
        try:
            cd = float(cd) if cd is not None else None
        except (TypeError, ValueError):
            cd = None
        self.delay[host] = min(max(self.min_delay, cd or 0.0), MAX_DELAY)
        self.next_at[host] = self.clock() + self.delay[host]  # robots.txt was just fetched
        return True

    def allowed_by_robots(self, url: str, host: str) -> bool:
        rp = self.robots.get(host)
        return bool(rp) and rp.can_fetch(USER_AGENT, url)

    def sitemap_urls(self, host: str, cap: int) -> list[str]:
        rp = self.robots.get(host)
        queue = list((rp.site_maps() if rp else None) or [f"https://{host}/sitemap.xml"])
        seen_maps, urls = set(), []
        while queue and len(seen_maps) < SITEMAP_MAX_FILES and len(urls) < cap * 2:
            sm = normalize_url(queue.pop(0))
            if not sm or sm in seen_maps or urlsplit(sm).hostname != host:
                continue
            seen_maps.add(sm)
            try:
                r = self._fetch(host, sm, accept="application/xml,text/xml,*/*;q=0.1")
            except FetchError:
                continue
            if r.status != 200 or not r.body:
                continue
            body = r.body
            if body[:2] == b"\x1f\x8b":
                try:
                    body = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(body, 20 * 1024 * 1024)
                except zlib.error:
                    continue
            try:
                root = ET.fromstring(body)
            except ET.ParseError:
                continue
            for el in root.iter():
                if not el.tag.endswith("loc") or not el.text:
                    continue
                loc = normalize_url(el.text.strip())
                if not loc or urlsplit(loc).hostname != host:
                    continue
                if root.tag.endswith("sitemapindex"):
                    queue.append(loc)
                elif not skip_reason(loc):
                    urls.append(loc)
        return urls[: cap * 2]

    def stop_host(self, host: str, status: str, msg: str):
        self.stopped.add(host)
        self.store.set_host(host, status=status, last_error=msg[:300], last_run=utcnow())
        print(f"  {host}: {status} — {msg}")

    def finish_host(self, host: str):
        if host in self.stopped:
            return
        if self.store.queued(host):
            self.store.set_host(host, status="crawling")  # continues tomorrow night
            return
        found = self.store.page_count(host)
        row = self.store.host_row(host)
        if found == 0 and row["status"] in ("first crawl", "new"):
            self.store.set_host(host, status="needs_review", found=0,
                                last_error=row["last_error"] or "first crawl stored no pages")
            return
        self.store.set_host(host, status="ok", found=found, cycle_completed=utcnow(),
                            last_success=utcnow())

    # -- main loop -------------------------------------------------------------
    def loop(self, hosts: list[str]):
        active = [h for h in hosts if h not in self.stopped]
        while active and self.clock() < self.deadline:
            active = [h for h in active if h not in self.stopped and self.store.queued(h)]
            if not active:
                break
            host = min(active, key=lambda h: self.next_at.get(h, 0))
            row = self.store.next_url(host)
            if row is None:
                continue
            self.crawl_one(host, row["url"], row["depth"])
            if len(self.pending_docs) + len(self.pending_deletes) >= FLUSH_EVERY:
                self.flush()
                self.write_status()

    def crawl_one(self, host: str, url: str, depth: int):
        h = self.hosts[host]
        st = self.store
        reason = skip_reason(url)
        if reason or not self.allowed_by_robots(url, host):
            st.mark(url, "skipped")
            st.bump_host(host, "skipped")
            self.progress.bump("skipped", host)
            return
        known = st.c.execute("SELECT 1 FROM pages WHERE url=?", (url,)).fetchone()
        if not known and st.page_count(host) >= h.page_cap:
            st.mark(url, "skipped")
            st.bump_host(host, "skipped")
            return
        try:
            r = self._fetch(host, url)
        except FetchError as e:
            st.mark(url, "error")
            if e.kind in ("unapproved", "not_https", "blocked_ip"):
                st.bump_host(host, "skipped")  # e.g. redirect off the approved list
                self.progress.bump("skipped", host)
                if known:
                    self.delete_page(url)
            else:
                st.bump_host(host, "errors")
                st.set_host(host, last_error=str(e)[:300])
                self.progress.bump("errors", host)
            return
        self.progress.bump("fetched", host)
        if self._challenged(r):
            st.mark(url, "error")
            self.stop_host(host, "challenged", "bot challenge / access denied — not bypassed")
            return
        if r.status == 429:
            st.mark(url, "queued")
            self.stop_host(host, "error", "asked us to slow down (429); resuming next run")
            return
        if r.status in (404, 410):
            st.mark(url, "gone")
            if known:
                self.delete_page(url)
            return
        if r.status != 200:
            st.mark(url, "error")
            st.bump_host(host, "errors")
            st.set_host(host, last_error=f"HTTP {r.status} on {url}"[:300])
            self.progress.bump("errors", host)
            return
        final = normalize_url(r.url) or url
        final_host = urlsplit(final).hostname
        if not self.approved(final_host or ""):
            st.mark(url, "skipped")
            return
        if r.content_type not in ("text/html", "application/xhtml+xml"):
            st.mark(url, "skipped")
            st.bump_host(host, "skipped")
            self.progress.bump("skipped", host)
            return
        page = extract(r.text(), final)
        st.mark(url, "done")
        if not page.nofollow and depth < MAX_DEPTH:
            budget = h.page_cap * 2 - st.frontier_count(host)
            for link in page.links:
                if budget <= 0:
                    break
                if urlsplit(link).hostname != host or skip_reason(link):
                    continue  # internal links only; http:// already dropped by normalize_url
                if st.enqueue(link, host, depth + 1):
                    budget -= 1
        canonical = page.canonical
        if canonical and urlsplit(canonical).hostname == final_host and self.approved(final_host):
            store_url = canonical
        else:
            store_url = final
        if page.noindex or not (page.body or page.title):
            st.bump_host(host, "skipped")
            self.progress.bump("skipped", host)
            if known:
                self.delete_page(url)
            return
        if store_url != url and known:
            self.delete_page(url)
        self.store_page(store_url, final_host, page)
        self.progress.bump("stored", host)

    @staticmethod
    def _challenged(r) -> bool:
        if r.headers.get("cf-mitigated", "").lower() == "challenge":
            return True
        if r.status in (403, 405, 429, 503):
            server = r.headers.get("server", "").lower()
            if any(s in server for s in ("cloudflare", "akamai", "ddos-guard", "sucuri", "imperva")):
                return True
            if any(k in r.headers for k in ("x-datadome", "x-amzn-waf-action", "x-px", "x-sucuri-id")):
                return True
        return False

    # -- storage -----------------------------------------------------------------
    def store_page(self, url: str, host: str, page) -> None:
        h = self.hosts[host]
        chash = hashlib.sha256(f"{DOC_VERSION}\n{page.title}\n{page.description}\n{page.body}".encode()).hexdigest()
        old = self.store.c.execute("SELECT content_hash, category FROM pages WHERE url=?", (url,)).fetchone()
        now = utcnow()
        self.store.c.execute(
            """INSERT INTO pages(url, id, host, title, description, headings, body, category, language,
                                 last_crawled, content_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET title=excluded.title, description=excluded.description,
                 headings=excluded.headings, body=excluded.body, category=excluded.category,
                 language=excluded.language, last_crawled=excluded.last_crawled,
                 content_hash=excluded.content_hash""",
            (url, doc_id(url), host, page.title, page.description, page.headings, page.body,
             h.category, page.language, now, chash))
        if old and old["content_hash"] == chash and old["category"] == h.category:
            return  # unchanged: no index write needed
        self.pending_docs.append(self.doc(url, host, page.title, page.description, page.headings,
                                          page.body, page.language, now, chash))

    def doc(self, url, host, title, description, headings, body, language, ts, chash) -> dict:
        h = self.hosts[host]
        return {"id": doc_id(url), "url": url, "host": host, "title": title,
                "description": description, "headings": headings, "body": body,
                "category": h.category, "language": language, "last_crawled": ts,
                "content_hash": chash, "boost": h.boost, "prefixes": url_prefixes(url)}

    def delete_page(self, url: str):
        self.store.c.execute("DELETE FROM pages WHERE url=?", (url,))
        self.pending_deletes.append(doc_id(url))

    def flush(self):
        if not self.meili:
            self.pending_docs.clear()
            self.pending_deletes.clear()
            return
        try:
            if self.pending_docs:
                self.meili.add_documents(WEB, self.pending_docs)
            if self.pending_deletes:
                self.meili.delete_documents(WEB, self.pending_deletes)
            self.pending_docs, self.pending_deletes = [], []
        except MeiliError as e:
            print(f"warning: index write failed ({e}); `crawler.py --reindex` will catch up")
            self.pending_docs, self.pending_deletes = [], []

    def reindex(self) -> int:
        """Push the whole page store (approved hosts only) into the index."""
        self.hosts = dict(self.cfg.hosts())
        self.purge_unapproved()
        if self.meili:
            for t in self.meili.ensure_indexes(self.cfg.filters()["synonyms"]):
                self.meili.wait(t)
        n = 0
        rows = self.store.c.execute("SELECT * FROM pages")
        prog = Progress("reindex")
        for r in rows:
            if not self.approved(r["host"]):
                continue
            self.pending_docs.append(self.doc(r["url"], r["host"], r["title"], r["description"],
                                              r["headings"], r["body"], r["language"],
                                              r["last_crawled"], r["content_hash"]))
            n += 1
            prog.bump("stored", r["host"])
            if len(self.pending_docs) >= 1000:
                self.flush()
        self.flush()
        prog.done()
        return n

    # -- status for the parent page --------------------------------------------
    def write_status(self, final: bool = False) -> dict:
        hosts = {}
        for r in self.store.c.execute("SELECT * FROM hosts"):
            hosts[r["host"]] = {
                "status": r["status"], "last_error": r["last_error"], "last_run": r["last_run"],
                "last_success": r["last_success"], "pages": self.store.page_count(r["host"]),
                "skipped": r["skipped"] or 0, "errors": r["errors"] or 0,
                "queued": self.store.queued(r["host"]),
            }
        if final:
            summary = {"started": self.started, "finished": utcnow(),
                       "totals": dict(self.progress.counts),
                       "problems": sorted(h for h, v in hosts.items()
                                          if v["status"] in ("blocked", "challenged", "needs_review", "error"))}
            self.store.c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_run', ?)",
                                 (json.dumps(summary),))
        row = self.store.c.execute("SELECT value FROM meta WHERE key='last_run'").fetchone()
        status = {"updated": utcnow(), "running": not final, "hosts": hosts,
                  "totals": dict(self.progress.counts),
                  "last_run": json.loads(row[0]) if row else None}
        path = self.cfg.settings.crawl_status_path
        try:
            atomic_write(path, json.dumps(status, indent=1), mode=0o640)
        except OSError as e:
            print(f"warning: could not write status: {e}")
        return status


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", action="append", help="crawl just this host now (repeatable)")
    ap.add_argument("--all", action="store_true", help="crawl every crawlable host now")
    ap.add_argument("--reindex", action="store_true", help="push stored pages into the index")
    ap.add_argument("--max-minutes", type=float,
                    default=float(os.environ.get("FS_CRAWL_MAX_MINUTES", DEFAULT_MAX_MINUTES)))
    args = ap.parse_args(argv)

    s = load_settings()
    cfg = ConfigStore(s)
    crawl_dir = Path(os.environ.get("FS_CRAWL_DIR", str(s.data_dir / "crawler")))
    crawl_dir.mkdir(parents=True, exist_ok=True)
    lock = open(crawl_dir / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another crawl is already running — exiting")
        return 0
    store = Store(crawl_dir / "crawl.db")
    meili = Meili(s.meili_url, s.meili_key, timeout=60)
    c = Crawler(cfg, store, meili, Fetcher(), max_minutes=args.max_minutes)

    if args.reindex:
        print(f"reindexed {c.reindex()} pages")
        return 0

    only = args.host
    force_all = args.all
    spool = s.spool_dir / "crawl-now"
    if spool.exists():  # a parent pressed "Crawl" on /parent
        try:
            req = json.loads(spool.read_text())
            if req.get("hosts"):
                only = sorted(set(only or []) | set(req["hosts"]))
            else:
                force_all = True
            print(f"crawl requested by a parent at {req.get('ts')}")
        except (ValueError, OSError):
            pass
        spool.unlink(missing_ok=True)
    status = c.run(only=only, force_all=force_all)
    bad = [h for h, v in status["hosts"].items() if v["status"] in ("blocked", "challenged", "needs_review")]
    if bad:
        print("needs David's review: " + ", ".join(sorted(bad)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
