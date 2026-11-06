"""Import simple.wikipedia.org from the official Wikimedia dump instead of
crawling it (allowlist.yaml: `source: wikimedia-dump`).

Links still point at the live page (https://simple.wikipedia.org/wiki/<Title>).
The dump's SHA-1 is checked against Wikimedia's published sha1sums file, and
the import is skipped when the dump has not changed since the last run.
"""
from __future__ import annotations

import bz2
import hashlib
import html
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

from safefetch import FetchError

DUMP_HOST = "dumps.wikimedia.org"
WIKI = "simplewiki"
BASE = f"https://{DUMP_HOST}/{WIKI}/latest/"
DUMP_FILE = f"{WIKI}-latest-pages-articles.xml.bz2"
SUMS_FILE = f"{WIKI}-latest-sha1sums.txt"
MAX_DUMP = 2 * 1024 * 1024 * 1024
MIN_BODY = 400

_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
_TABLE = re.compile(r"\{\|.*?\|\}", re.S)
_REF = re.compile(r"<ref[^>/]*/>|<ref[^>]*>.*?</ref>", re.S | re.I)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TAG = re.compile(r"<[^>]+>")
_FILE_LINK = re.compile(r"\[\[(?:File|Image|Category|Media):[^\[\]]*(?:\[\[[^\[\]]*\]\][^\[\]]*)*\]\]", re.I)
_INTERWIKI = re.compile(r"\[\[[a-z\-]{2,12}:[^\]]*\]\]")
_LINK = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]")
_EXT = re.compile(r"\[https?://[^\s\]]+\s*([^\]]*)\]")
_HEADING = re.compile(r"^(={2,6})\s*(.*?)\s*\1\s*$", re.M)
_QUOTES = re.compile(r"'{2,5}")
_LIST = re.compile(r"^[*#:;]+\s*", re.M)
_MAGIC = re.compile(r"__[A-Z]+__")


def strip_wikitext(text: str) -> tuple[str, list[str]]:
    t = _COMMENT.sub("", text)
    t = _REF.sub("", t)
    for _ in range(6):  # nested templates
        t2 = _TEMPLATE.sub("", t)
        if t2 == t:
            break
        t = t2
    t = _TABLE.sub("", t)
    t = _FILE_LINK.sub("", t)
    t = _INTERWIKI.sub("", t)
    t = _LINK.sub(r"\1", t)
    t = _EXT.sub(r"\1", t)
    headings = [h[1] for h in _HEADING.findall(t)
                if h[1].lower() not in ("references", "other websites", "related pages", "notes",
                                        "further reading", "sources")]
    t = _HEADING.sub(r"\n\2\n", t)
    t = _QUOTES.sub("", t)
    t = _LIST.sub("", t)
    t = _MAGIC.sub("", t)
    t = _TAG.sub("", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t).strip()
    return t, headings


def first_sentences(body: str, n: int = 300) -> str:
    s = body.replace("\n", " ")
    if len(s) <= n:
        return s
    cut = s.rfind(". ", 0, n)
    return s[: cut + 1] if cut > 80 else s[:n].rstrip() + "…"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def import_dump(crawler, host: str) -> None:
    from crawler import Progress, doc_id, utcnow  # local import: avoids a cycle
    from extract import clean

    h = crawler.hosts[host]
    store = crawler.store
    work = Path(crawler.cfg.settings.data_dir) / "crawler" / "dumps"
    work.mkdir(parents=True, exist_ok=True)
    approved = {DUMP_HOST}
    print(f"  {host}: checking Wikimedia dump")
    sums = crawler.fetcher.get(BASE + SUMS_FILE, approved, accept="text/plain", max_bytes=256 * 1024)
    if sums.status != 200:
        raise FetchError("network", f"sha1sums returned {sums.status}")
    expected = None
    for line in sums.text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].endswith("-pages-articles.xml.bz2"):
            expected = parts[0].lower()
    if not expected or not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise FetchError("network", "no checksum published for the dump")
    row = store.host_row(host)
    if row["dump_sha1"] == expected and store.page_count(host):
        print(f"  {host}: dump unchanged since last import")
        store.set_host(host, status="ok", last_run=utcnow(), last_success=utcnow(),
                       cycle_completed=utcnow())
        return

    dest = work / DUMP_FILE
    tmp = dest.with_suffix(".part")
    digest = hashlib.sha1()
    got = [0]
    t0 = time.monotonic()
    last = [0.0]
    with open(tmp, "wb") as fh:
        def sink(chunk: bytes):
            fh.write(chunk)
            digest.update(chunk)
            got[0] += len(chunk)
            now = time.monotonic()
            if now - last[0] > (0.5 if crawler.progress.tty else 30):
                last[0] = now
                mb = got[0] / 1e6
                print(f"\r  downloading dump: {mb:,.0f} MB ({mb / max(now - t0, 1):.1f} MB/s)",
                      end="" if crawler.progress.tty else "\n", flush=True)
        r = crawler.fetcher.get(BASE + DUMP_FILE, approved, accept="*/*", max_bytes=MAX_DUMP,
                                timeout=4 * 3600, sink=sink)
    print()
    if r.status != 200:
        tmp.unlink(missing_ok=True)
        raise FetchError("network", f"dump returned {r.status}")
    if digest.hexdigest() != expected:
        tmp.unlink(missing_ok=True)
        raise FetchError("network", "dump checksum mismatch — not imported")
    tmp.replace(dest)
    print(f"  {host}: dump verified (sha1 {expected[:12]}…), importing up to {h.page_cap:,} articles")

    prog = Progress("wikidump")
    kept: set[str] = set()
    with bz2.open(dest, "rb") as fh:
        ns = title = text = None
        redirect = False
        for event, el in ET.iterparse(fh, events=("end",)):
            tag = _local(el.tag)
            if tag == "ns":
                ns = el.text
            elif tag == "title":
                title = el.text
            elif tag == "redirect":
                redirect = True
            elif tag == "text":
                text = el.text or ""
            elif tag == "page":
                if ns == "0" and not redirect and title and text:
                    body, heads = strip_wikitext(text)
                    if len(body) >= MIN_BODY:
                        url = f"https://{host}/wiki/" + quote(title.replace(" ", "_"), safe="_()',-.:")
                        page = type("P", (), {})()
                        page.title = clean(title, 200)
                        page.body = clean(body, 20_000)
                        page.description = clean(first_sentences(body), 400)
                        page.headings = clean(" · ".join(heads), 2000)
                        page.language = "en"
                        crawler.store_page(url, host, page)
                        kept.add(doc_id(url))
                        prog.bump("stored", host)
                        if len(crawler.pending_docs) >= 500:
                            crawler.flush()
                        if len(kept) >= h.page_cap:
                            break
                ns = title = text = None
                redirect = False
                el.clear()
    crawler.flush()
    prog.done()
    stale = [r["url"] for r in store.c.execute("SELECT url, id FROM pages WHERE host=?", (host,))
             if r["id"] not in kept]
    for u in stale:
        crawler.delete_page(u)
    crawler.flush()
    dest.unlink(missing_ok=True)  # text is kept in the page store; the dump is not needed
    store.set_host(host, status="ok", dump_sha1=expected, last_run=utcnow(), last_success=utcnow(),
                   cycle_completed=utcnow(), found=len(kept), last_error=None)
    print(f"  {host}: {len(kept):,} articles indexed, {len(stale):,} removed")
