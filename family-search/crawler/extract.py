"""Turn an HTML page into searchable text. Stores text, never markup.

Uses only the stdlib parser. Everything extracted is plain text; the backend
and frontend still treat it as untrusted (escaped, never rendered as HTML).
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

BODY_LIMIT = 20_000
HEADINGS_LIMIT = 2_000
DESC_LIMIT = 400
TITLE_LIMIT = 200

SKIP_TEXT_TAGS = {"script", "style", "noscript", "template", "svg", "math", "iframe",
                  "object", "canvas", "select", "button", "form", "nav", "footer", "aside"}
HEADING_TAGS = {"h1", "h2", "h3"}
BLOCK_TAGS = {"p", "div", "li", "br", "tr", "section", "article", "h1", "h2", "h3", "h4",
              "h5", "h6", "blockquote", "dd", "dt", "td", "th", "header", "main", "figcaption"}

TRACKING_PARAMS = re.compile(
    r"^(utm_[a-z_]+|fbclid|gclid|gclsrc|dclid|msclkid|yclid|mc_cid|mc_eid|igshid|_ga|_gl|"
    r"ref|ref_src|ref_url|spm|si|cmpid|cid|s_cid|icid|ncid|sr_share|share|shared|from|"
    r"hsenc|_hsenc|_hsmi|mkt_tok|trk|trkcampaign|oly_enc_id|oly_anon_id|vero_id|wt\.mc_id)$",
    re.I)

SKIP_PATH = re.compile(
    r"(^|/)(login|log-in|signin|sign-in|signup|sign-up|register|registration|logout|"
    r"account|accounts|my-account|myaccount|profile|checkout|cart|basket|donate-now|"
    r"comment|comments|forum|forums|discussion|discussions|tag|tags|print|printable|"
    r"wp-admin|wp-login|wp-json|feed|rss|share|subscribe|password|auth|oauth|sso|"
    r"user|users|members?|search|my)(/|$|\.)", re.I)
SKIP_QUERY = re.compile(r"(^|&)(replytocom|print|share|action|returnurl|return_to|redirect|"
                        r"login|session|sid|phpsessid|jsessionid|format=print)=", re.I)
NON_HTML_EXT = re.compile(
    r"\.(pdf|jpe?g|png|gif|webp|svg|ico|bmp|tiff?|mp[34]|m4a|wav|ogg|webm|mov|avi|zip|gz|tgz|"
    r"rar|7z|exe|dmg|apk|msi|docx?|xlsx?|pptx?|csv|json|xml|rss|atom|css|js|woff2?|ttf|eot|"
    r"epub|mobi|ics)$", re.I)

_WS = re.compile(r"\s+")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean(s: str, limit: int | None = None) -> str:
    s = _WS.sub(" ", _CTRL.sub("", s or "")).strip()
    return s[:limit] if limit else s


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Absolute, https-only, fragment-free, tracking-free, sorted query."""
    try:
        if base:
            url = urljoin(base, url.strip())
        p = urlsplit(url.strip())
    except ValueError:
        return None
    if p.scheme.lower() != "https" or not p.hostname:
        return None
    try:
        if p.port not in (None, 443):
            return None
    except ValueError:
        return None
    if p.username or p.password:
        return None
    host = p.hostname.lower().rstrip(".")
    path = re.sub(r"/{2,}", "/", p.path or "/")
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not TRACKING_PARAMS.match(k)]
    query.sort()
    return urlunsplit(("https", host, path, urlencode(query, doseq=True), ""))


def skip_reason(url: str) -> str | None:
    p = urlsplit(url)
    if NON_HTML_EXT.search(p.path):
        return "not html"
    if SKIP_PATH.search(p.path):
        return "skipped page type"
    if p.query and SKIP_QUERY.search(p.query):
        return "skipped page type"
    if len(url) > 1000:
        return "url too long"
    return None


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.headings: list[str] = []
        self.body: list[str] = []
        self.links: list[str] = []
        self.description = ""
        self.og_description = ""
        self.canonical = ""
        self.lang = ""
        self.robots = ""
        self.base = ""
        self._skip = 0
        self._in_title = False
        self._in_heading = 0
        self._heading_buf: list[str] = []
        self._body_len = 0

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html" and a.get("lang"):
            self.lang = a["lang"][:12].lower()
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            if name == "description":
                self.description = a.get("content", "")
            elif name == "og:description":
                self.og_description = a.get("content", "")
            elif name in ("robots", "strzfamilysearch"):
                self.robots += " " + a.get("content", "").lower()
        elif tag == "link":
            rel = a.get("rel", "").lower().split()
            if "canonical" in rel and a.get("href"):
                self.canonical = a["href"]
        elif tag == "base" and a.get("href") and not self.base:
            self.base = a["href"]
        elif tag == "a":
            href = a.get("href", "")
            rel = a.get("rel", "").lower()
            if href and "nofollow" not in rel and len(self.links) < 2000:
                self.links.append(href)
        if tag in SKIP_TEXT_TAGS:
            self._skip += 1
        elif tag in HEADING_TAGS and not self._skip:
            self._in_heading += 1
        if tag in BLOCK_TAGS:
            self._add_body(" ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in SKIP_TEXT_TAGS and self._skip:
            self._skip -= 1
        elif tag in HEADING_TAGS and self._in_heading:
            self._in_heading -= 1
            if not self._in_heading:
                h = clean("".join(self._heading_buf))
                if h:
                    self.headings.append(h)
                self._heading_buf = []
        if tag in BLOCK_TAGS:
            self._add_body(" ")

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip:
            return
        if self._in_heading:
            self._heading_buf.append(data)
        self._add_body(data)

    def _add_body(self, s: str):
        if self._body_len < BODY_LIMIT * 2:
            self.body.append(s)
            self._body_len += len(s)


class Page:
    __slots__ = ("title", "description", "headings", "body", "links", "canonical",
                 "language", "noindex", "nofollow")


def extract(html: str, page_url: str) -> Page:
    p = _Parser()
    try:
        p.feed(html)
        p.close()
    except Exception:  # malformed markup: keep whatever was parsed
        pass
    base = urljoin(page_url, p.base) if p.base else page_url
    pg = Page()
    pg.title = clean("".join(p.title_parts), TITLE_LIMIT)
    pg.description = clean(p.description or p.og_description, DESC_LIMIT)
    pg.headings = clean(" · ".join(dict.fromkeys(p.headings)), HEADINGS_LIMIT)
    pg.body = clean("".join(p.body), BODY_LIMIT)
    pg.language = clean(p.lang, 12) or "en"
    pg.noindex = "noindex" in p.robots or "none" in p.robots.split()
    pg.nofollow = "nofollow" in p.robots or "none" in p.robots.split()
    pg.canonical = normalize_url(p.canonical, base) if p.canonical else None
    links = []
    for href in p.links:
        if href.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
            continue
        n = normalize_url(href, base)
        if n:
            links.append(n)
    pg.links = list(dict.fromkeys(links))
    return pg
