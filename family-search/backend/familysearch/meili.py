"""Minimal Meilisearch client (v1.x HTTP API), localhost only.

Used by both the backend and the crawler. The index is a disposable cache:
scripts/rebuild-index.sh recreates it from SQLite, config and the crawler's
page store.
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterable

import httpx

WEB = "web_pages"
LIBRARY = "library_items"
PHOTOS = "photos"

MARK_PRE = ""
MARK_POST = ""

INDEX_SETTINGS: dict[str, dict[str, Any]] = {
    WEB: {
        "searchableAttributes": ["title", "headings", "description", "body"],
        "filterableAttributes": ["host", "category", "prefixes"],
        "sortableAttributes": ["boost"],
        "rankingRules": ["words", "typo", "boost:desc", "proximity", "attribute", "sort", "exactness"],
        "typoTolerance": {"enabled": True},
    },
    LIBRARY: {
        "searchableAttributes": ["title", "note", "body"],
        "filterableAttributes": ["kind"],
        "typoTolerance": {"enabled": True},
    },
    PHOTOS: {
        "searchableAttributes": ["album_name", "description", "date_text"],
        "filterableAttributes": ["album_id"],
        "sortableAttributes": ["taken_date"],
        "typoTolerance": {"enabled": True},
    },
}


class MeiliError(RuntimeError):
    pass


def quote_filter_value(v: str) -> str:
    # Values we put in filters are validated hostnames / UUIDs, but escape anyway.
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def in_filter(field: str, values: Iterable[str]) -> str:
    vals = sorted(set(values))
    return f"{field} IN [{', '.join(quote_filter_value(v) for v in vals)}]"


class Meili:
    def __init__(self, url: str, key: str | None, timeout: float = 10.0,
                 transport: httpx.BaseTransport | None = None):
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        self._c = httpx.Client(base_url=url, headers=headers, timeout=timeout,
                               follow_redirects=False, trust_env=False, transport=transport)

    def _req(self, method: str, path: str, **kw) -> Any:
        try:
            r = self._c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise MeiliError(f"search index unreachable: {type(e).__name__}") from None
        if r.status_code == 404 and method == "GET":
            return None
        if r.status_code >= 400:
            try:
                msg = r.json().get("code", "")
            except ValueError:
                msg = ""
            raise MeiliError(f"index error {r.status_code} {msg}")
        return r.json() if r.content else None

    # -- admin -----------------------------------------------------------
    def healthy(self) -> bool:
        try:
            return (self._req("GET", "/health") or {}).get("status") == "available"
        except MeiliError:
            return False

    def ensure_indexes(self, synonyms: dict[str, list[str]] | None = None) -> list[int]:
        tasks = []
        for uid, settings in INDEX_SETTINGS.items():
            if self._req("GET", f"/indexes/{uid}") is None:
                t = self._req("POST", "/indexes", json={"uid": uid, "primaryKey": "id"})
                tasks.append(t["taskUid"])
            body = dict(settings)
            body["synonyms"] = expand_synonyms(synonyms or {})
            t = self._req("PATCH", f"/indexes/{uid}/settings", json=body)
            tasks.append(t["taskUid"])
        return tasks

    def set_synonyms(self, synonyms: dict[str, list[str]]) -> None:
        body = expand_synonyms(synonyms)
        for uid in INDEX_SETTINGS:
            self._req("PUT", f"/indexes/{uid}/settings/synonyms", json=body)

    def delete_index(self, uid: str) -> int | None:
        t = self._req("DELETE", f"/indexes/{uid}")
        return t["taskUid"] if t else None

    def wait(self, task_uid: int, timeout: float = 120.0) -> dict:
        end = time.monotonic() + timeout
        while True:
            t = self._req("GET", f"/tasks/{task_uid}") or {}
            if t.get("status") in ("succeeded", "failed", "canceled"):
                return t
            if time.monotonic() > end:
                raise MeiliError(f"task {task_uid} still {t.get('status')}")
            time.sleep(0.2)

    # -- documents -------------------------------------------------------
    def add_documents(self, uid: str, docs: list[dict]) -> int | None:
        if not docs:
            return None
        return self._req("POST", f"/indexes/{uid}/documents", content=json.dumps(docs))["taskUid"]

    def update_documents(self, uid: str, docs: list[dict]) -> int | None:
        if not docs:
            return None
        return self._req("PUT", f"/indexes/{uid}/documents", content=json.dumps(docs))["taskUid"]

    def delete_documents(self, uid: str, ids: list[str]) -> int | None:
        if not ids:
            return None
        return self._req("POST", f"/indexes/{uid}/documents/delete-batch", json=ids)["taskUid"]

    def delete_by_filter(self, uid: str, flt: str) -> int:
        return self._req("POST", f"/indexes/{uid}/documents/delete", json={"filter": flt})["taskUid"]

    def iter_documents(self, uid: str, flt: str | None = None, fields: list[str] | None = None,
                       batch: int = 1000):
        offset = 0
        while True:
            body: dict[str, Any] = {"offset": offset, "limit": batch}
            if flt:
                body["filter"] = flt
            if fields:
                body["fields"] = fields
            r = self._req("POST", f"/indexes/{uid}/documents/fetch", json=body)
            results = (r or {}).get("results", [])
            yield from results
            if len(results) < batch:
                return
            offset += batch

    def search(self, uid: str, body: dict) -> dict:
        return self._req("POST", f"/indexes/{uid}/search", json=body) or {}


def expand_synonyms(syn: dict[str, list[str]]) -> dict[str, list[str]]:
    """Meilisearch synonyms are one-way; make the parent's list work both ways."""
    out: dict[str, set[str]] = {}
    for k, vals in syn.items():
        out.setdefault(k, set()).update(vals)
        for v in vals:
            out.setdefault(v, set()).add(k)
    return {k: sorted(v) for k, v in out.items()}
