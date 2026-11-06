"""Cloudflare Access JWT validation, roles, CSRF, rate limits.

Cloudflare Access is the outer wall, but the app never trusts that it ran:
every request must carry a Cf-Access-Jwt-Assertion whose signature, audience,
issuer and expiry all check out against the team's published keys. No other
header is trusted for identity.
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import threading
import time
from dataclasses import dataclass
from typing import Callable

import httpx
import jwt

JWT_HEADER = "cf-access-jwt-assertion"
LEEWAY_SECONDS = 30
JWKS_TTL = 3600
JWKS_MIN_REFRESH = 60


class AuthError(Exception):
    def __init__(self, reason: str, status: int = 401):
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class Identity:
    email: str
    is_parent: bool


def _fetch_jwks_http(url: str) -> dict:
    r = httpx.get(url, timeout=10, follow_redirects=False,
                  headers={"User-Agent": "StrzFamilySearch/1.0"})
    r.raise_for_status()
    return r.json()


class AccessVerifier:
    def __init__(self, team_domain: str, audience: str,
                 fetch_jwks: Callable[[str], dict] = _fetch_jwks_http):
        self.issuer = f"https://{team_domain}"
        self.certs_url = f"{self.issuer}/cdn-cgi/access/certs"
        self.audience = audience
        self._fetch = fetch_jwks
        self._lock = threading.Lock()
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at = 0.0

    def _refresh(self, force: bool) -> None:
        now = time.monotonic()
        with self._lock:
            age = now - self._fetched_at
            if self._keys and not force and age < JWKS_TTL:
                return
            if force and age < JWKS_MIN_REFRESH and self._keys:
                return
            doc = self._fetch(self.certs_url)
            keys = {}
            for k in doc.get("keys", []):
                if k.get("kty") != "RSA" or not k.get("kid"):
                    continue
                keys[k["kid"]] = jwt.PyJWK(k, algorithm="RS256")
            if not keys:
                raise AuthError("no signing keys published", 503)
            self._keys = keys
            self._fetched_at = now

    def _key_for(self, kid: str) -> jwt.PyJWK:
        self._refresh(force=False)
        if kid not in self._keys:
            self._refresh(force=True)
        if kid not in self._keys:
            raise AuthError("unknown signing key")
        return self._keys[kid]

    def verify(self, token: str) -> dict:
        if not token or len(token) > 8192:
            raise AuthError("missing token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AuthError("malformed token")
        if header.get("alg") != "RS256":
            raise AuthError("unexpected algorithm")
        try:
            key = self._key_for(str(header.get("kid", "")))
        except AuthError:
            raise
        except Exception:
            raise AuthError("signing keys unavailable", 503)
        try:
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], audience=self.audience,
                issuer=self.issuer, leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud"], "verify_signature": True},
            )
        except jwt.ExpiredSignatureError:
            raise AuthError("expired token")
        except jwt.InvalidAudienceError:
            raise AuthError("wrong audience")
        except jwt.InvalidIssuerError:
            raise AuthError("wrong issuer")
        except jwt.PyJWTError:
            raise AuthError("invalid token")
        email = claims.get("email")
        if not isinstance(email, str) or "@" not in email:
            # Service tokens carry no email; nothing here is meant for them.
            raise AuthError("no user email", 403)
        return claims


def identify(claims: dict, parent_emails: frozenset[str],
             allowed_emails: frozenset[str]) -> Identity:
    email = claims["email"].strip().lower()
    is_parent = email in parent_emails
    if allowed_emails and not is_parent and email not in allowed_emails:
        raise AuthError("email not allowed", 403)
    return Identity(email=email, is_parent=is_parent)


# --------------------------------------------------------------------------
# CSRF: a token bound to the signed-in email, sent back in X-CSRF-Token.
# Combined with the Origin / Sec-Fetch-Site check in main.py.
# --------------------------------------------------------------------------

def csrf_token(secret: str, email: str) -> str:
    return hmac.new(secret.encode(), f"csrf:{email}".encode(), hashlib.sha256).hexdigest()


def csrf_ok(secret: str, email: str, presented: str | None) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(csrf_token(secret, email), presented)


# --------------------------------------------------------------------------
# Rate limiting (per user, sliding one-minute window, in memory)
# --------------------------------------------------------------------------

class RateLimiter:
    def __init__(self):
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, str], collections.deque] = {}

    def allow(self, bucket: str, key: str, per_minute: int) -> bool:
        now = time.monotonic()
        with self._lock:
            dq = self._hits.setdefault((bucket, key), collections.deque())
            while dq and now - dq[0] > 60:
                dq.popleft()
            if len(dq) >= per_minute:
                return False
            dq.append(now)
            if len(self._hits) > 10_000:  # bound memory
                for k in [k for k, v in self._hits.items() if not v][:5000]:
                    del self._hits[k]
            return True
