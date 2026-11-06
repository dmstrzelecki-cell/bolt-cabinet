"""HTTPS-only fetcher that cannot be talked into reaching anything private.

* https:// only, port 443 only, no userinfo.
* The hostname must be EXACTLY in the approved set (a parent domain does not
  approve its subdomains) — checked for the first URL and for every redirect
  hop before it is followed.
* DNS is resolved by us before each connection; if ANY address for the name is
  private, loopback, link-local, CGNAT, multicast, reserved or a metadata
  range, the fetch is refused. The socket then connects to the exact IP we
  checked (with SNI + certificate verification against the hostname), so a
  DNS answer cannot change between the check and the connect.
* Response size and total time are capped.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable, Iterable
from urllib.parse import urljoin, urlsplit

USER_AGENT = "StrzFamilySearch/1.0 (private family index)"
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = 20.0
MAX_REDIRECTS = 5

_BLOCKED = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
    "255.255.255.255/32",
    "::/128", "::1/128", "::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48", "100::/64",
    "2001::/32", "2001:db8::/32", "2002::/16", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8",
    # cloud metadata endpoints (also covered above, listed for clarity)
    "169.254.169.254/32", "fd00:ec2::254/128",
)]


class FetchError(Exception):
    """kind: blocked_ip | dns | unapproved | not_https | redirect_loop |
    too_large | timeout | tls | network | bad_url | not_html"""

    def __init__(self, kind: str, msg: str = ""):
        super().__init__(f"{kind}: {msg}" if msg else kind)
        self.kind = kind


def ip_allowed(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if not addr.is_global:
        return False
    return not any(addr in n for n in _BLOCKED if n.version == addr.version)


def system_resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(i[4][0] for i in infos))


@dataclass
class Response:
    url: str                      # final URL after redirects
    status: int
    headers: dict[str, str]
    body: bytes = b""
    redirects: list[str] = field(default_factory=list)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def text(self) -> str:
        ct = self.headers.get("content-type", "")
        enc = "utf-8"
        if "charset=" in ct:
            enc = ct.split("charset=", 1)[1].split(";")[0].strip().strip('"') or "utf-8"
        try:
            return self.body.decode(enc, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


class _Pinned(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, timeout: float, context: ssl.SSLContext):
        super().__init__(host, 443, timeout=timeout, context=context)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def check_url(url: str, approved: Iterable[str] | Callable[[str], bool]) -> str:
    try:
        p = urlsplit(url)
    except ValueError:
        raise FetchError("bad_url", url[:100])
    if p.scheme != "https":
        raise FetchError("not_https", url[:100])
    if p.username or p.password:
        raise FetchError("bad_url", "userinfo in URL")
    try:
        port = p.port
    except ValueError:
        raise FetchError("bad_url", "bad port")
    if port not in (None, 443):
        raise FetchError("bad_url", "non-standard port")
    host = (p.hostname or "").lower().rstrip(".")
    ok = approved(host) if callable(approved) else host in set(approved)
    if not host or not ok:
        raise FetchError("unapproved", host)
    return host


class Fetcher:
    def __init__(self, resolve: Callable[[str], list[str]] = system_resolve,
                 ip_ok: Callable[[str], bool] = ip_allowed,
                 context: ssl.SSLContext | None = None,
                 user_agent: str = USER_AGENT):
        self.resolve = resolve
        self.ip_ok = ip_ok
        self.context = context or ssl.create_default_context()
        self.ua = user_agent

    # Overridable in tests: one HTTP exchange against an already-vetted IP.
    def _exchange(self, host: str, ip: str, path: str, deadline: float, max_bytes: int,
                  accept: str, sink=None) -> Response:
        remaining = max(1.0, deadline - time.monotonic())
        conn = _Pinned(host, ip, timeout=min(remaining, TIMEOUT), context=self.context)
        try:
            conn.request("GET", path or "/", headers={
                "Host": host, "User-Agent": self.ua, "Accept": accept,
                "Accept-Encoding": "gzip, deflate", "Connection": "close",
            })
            r = conn.getresponse()
            headers = {k.lower(): v for k, v in r.getheaders()}
            if r.status in (301, 302, 303, 307, 308) or r.status >= 400 or (
                    accept.startswith("text/html") and not _is_html(headers)):
                return Response(url="", status=r.status, headers=headers)
            cl = headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > max_bytes:
                raise FetchError("too_large", cl)
            chunks, total = [], 0
            while True:
                if time.monotonic() > deadline:
                    raise FetchError("timeout")
                chunk = r.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError("too_large")
                if sink:
                    sink(chunk)
                else:
                    chunks.append(chunk)
            body = b"".join(chunks)
            if not sink:
                body = _decode(body, headers.get("content-encoding", ""), max_bytes)
            return Response(url="", status=r.status, headers=headers, body=body)
        except ssl.SSLError as e:
            raise FetchError("tls", type(e).__name__)
        except socket.timeout:
            raise FetchError("timeout")
        except (OSError, http.client.HTTPException) as e:
            raise FetchError("network", type(e).__name__)
        finally:
            conn.close()

    def _vet(self, host: str) -> str:
        try:
            ips = self.resolve(host)
        except (socket.gaierror, UnicodeError, OSError):
            raise FetchError("dns", host)
        if not ips:
            raise FetchError("dns", host)
        bad = [ip for ip in ips if not self.ip_ok(ip)]
        if bad:
            raise FetchError("blocked_ip", f"{host} -> non-public address")
        return ips[0]

    def get(self, url: str, approved: Iterable[str] | Callable[[str], bool],
            accept: str = "text/html,application/xhtml+xml", max_bytes: int = MAX_BYTES,
            timeout: float = TIMEOUT, sink=None) -> Response:
        """Fetch url, validating every redirect hop. Raises FetchError."""
        deadline = time.monotonic() + timeout
        seen: list[str] = []
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            host = check_url(current, approved)
            ip = self._vet(host)
            p = urlsplit(current)
            path = (p.path or "/") + (f"?{p.query}" if p.query else "")
            resp = self._exchange(host, ip, path, deadline, max_bytes, accept, sink)
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location")
                if not loc:
                    raise FetchError("bad_url", "redirect without location")
                nxt = urljoin(current, loc)
                seen.append(current)
                if nxt in seen:
                    raise FetchError("redirect_loop")
                current = nxt  # validated by check_url at the top of the loop
                continue
            resp.url = current
            resp.redirects = seen
            return resp
        raise FetchError("redirect_loop", "too many redirects")


def _is_html(headers: dict[str, str]) -> bool:
    ct = headers.get("content-type", "").split(";")[0].strip().lower()
    return ct in ("text/html", "application/xhtml+xml")


def _decode(body: bytes, encoding: str, max_bytes: int) -> bytes:
    enc = encoding.strip().lower()
    if enc in ("", "identity"):
        return body
    if enc in ("gzip", "x-gzip", "deflate"):
        wbits = 16 + zlib.MAX_WBITS if enc != "deflate" else zlib.MAX_WBITS
        d = zlib.decompressobj(wbits)
        try:
            out = d.decompress(body, max_bytes + 1)
        except zlib.error:
            if enc == "deflate":  # raw deflate
                d = zlib.decompressobj(-zlib.MAX_WBITS)
                out = d.decompress(body, max_bytes + 1)
            else:
                raise FetchError("network", "bad gzip")
        if len(out) > max_bytes or d.unconsumed_tail:
            raise FetchError("too_large", "decompressed")
        return out
    raise FetchError("network", f"unsupported encoding {enc}")

