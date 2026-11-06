"""A small in-memory stand-in for the Meilisearch v1 HTTP API, used through an
httpx.MockTransport. Implements only what Family Search calls. Matching is
deliberately simple (every query word must appear in a searchable field)."""
from __future__ import annotations

import json
import math
import re

import httpx

PRE, POST = "", ""


def _parse_filter(flt: str):
    """Supports `field IN ["a","b"]`, `field = "a"`, joined by AND."""
    if not flt:
        return lambda d: True
    preds = []
    for part in re.split(r"\s+AND\s+", flt):
        m = re.match(r'^\s*(\w+)\s+IN\s+\[(.*)\]\s*$', part)
        if m:
            vals = set(json.loads("[" + m.group(2) + "]"))
            preds.append(lambda d, f=m.group(1), v=vals: d.get(f) in v)
            continue
        m = re.match(r'^\s*(\w+)\s*=\s*(".*")\s*$', part)
        if m:
            val = json.loads(m.group(2))
            preds.append(lambda d, f=m.group(1), v=val: d.get(f) == v)
            continue
        raise ValueError(f"unsupported filter {part!r}")
    return lambda d: all(p(d) for p in preds)


class FakeMeili:
    def __init__(self):
        self.indexes: dict[str, dict] = {}
        self.settings: dict[str, dict] = {}
        self.task = 0
        self.requests: list[tuple[str, str]] = []
        self.ignore_deletes = False   # simulate a stale index
        self.down = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _task(self):
        self.task += 1
        return httpx.Response(202, json={"taskUid": self.task, "status": "enqueued"})

    def handle(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down")
        path = req.url.path
        self.requests.append((req.method, path))
        body = json.loads(req.content or b"null") if req.content else None
        if path == "/health":
            return httpx.Response(200, json={"status": "available"})
        if m := re.match(r"^/tasks/(\d+)$", path):
            return httpx.Response(200, json={"uid": int(m.group(1)), "status": "succeeded"})
        if path == "/indexes" and req.method == "POST":
            self.indexes.setdefault(body["uid"], {})
            return self._task()
        m = re.match(r"^/indexes/([^/]+)(/.*)?$", path)
        if not m:
            return httpx.Response(404, json={"code": "not_found"})
        uid, rest = m.group(1), m.group(2) or ""
        if rest == "" and req.method == "GET":
            if uid not in self.indexes:
                return httpx.Response(404, json={"code": "index_not_found"})
            return httpx.Response(200, json={"uid": uid, "primaryKey": "id"})
        if rest == "" and req.method == "DELETE":
            self.indexes.pop(uid, None)
            return self._task()
        idx = self.indexes.setdefault(uid, {})
        if rest == "/settings" and req.method == "PATCH":
            self.settings.setdefault(uid, {}).update(body)
            return self._task()
        if rest == "/settings/synonyms":
            self.settings.setdefault(uid, {})["synonyms"] = body
            return self._task()
        if rest == "/documents" and req.method == "POST":
            for d in body:
                idx[str(d["id"])] = dict(d)
            return self._task()
        if rest == "/documents" and req.method == "PUT":
            for d in body:
                if str(d["id"]) in idx:
                    idx[str(d["id"])].update(d)
                else:
                    idx[str(d["id"])] = dict(d)
            return self._task()
        if rest == "/documents/delete-batch":
            if not self.ignore_deletes:
                for i in body:
                    idx.pop(str(i), None)
            return self._task()
        if rest == "/documents/delete":
            if not self.ignore_deletes:
                pred = _parse_filter(body["filter"])
                for k in [k for k, d in idx.items() if pred(d)]:
                    del idx[k]
            return self._task()
        if rest == "/documents/fetch":
            pred = _parse_filter(body.get("filter") or "")
            docs = [d for d in idx.values() if pred(d)]
            off, lim = body.get("offset", 0), body.get("limit", 20)
            out = docs[off:off + lim]
            if body.get("fields"):
                out = [{k: d.get(k) for k in body["fields"]} for d in out]
            return httpx.Response(200, json={"results": out, "total": len(docs)})
        if rest == "/search":
            return httpx.Response(200, json=self.search(uid, body))
        return httpx.Response(404, json={"code": "unknown_route", "path": path})

    def search(self, uid: str, body: dict) -> dict:
        idx = self.indexes.get(uid, {})
        searchable = (self.settings.get(uid, {}).get("searchableAttributes")
                      or ["title", "body", "description", "note", "album_name", "date_text"])
        if body.get("attributesToSearchOn"):
            searchable = body["attributesToSearchOn"]
        words = [w for w in re.findall(r"\w+", (body.get("q") or "").lower())]
        pred = _parse_filter(body.get("filter") or "")
        hits = []
        for d in idx.values():
            if not pred(d):
                continue
            hay = " ".join(str(d.get(a, "")) for a in searchable).lower()
            if all(w in hay for w in words):
                hits.append(d)
        for s in reversed(body.get("sort") or []):
            field, _, order = s.partition(":")
            hits.sort(key=lambda d: str(d.get(field, "")), reverse=(order == "desc"))
        if not body.get("sort"):
            hits.sort(key=lambda d: -int(d.get("boost") or 0))
        total = len(hits)
        if "page" in body or "hitsPerPage" in body:
            per = body.get("hitsPerPage", 20)
            page = body.get("page", 1)
            sel = hits[(page - 1) * per: page * per]
            meta = {"totalHits": total, "totalPages": math.ceil(total / per) if per else 0,
                    "page": page, "hitsPerPage": per}
        else:
            off, lim = body.get("offset", 0), body.get("limit", 20)
            sel = hits[off:off + lim]
            meta = {"estimatedTotalHits": total, "offset": off, "limit": lim}
        out = []
        for d in sel:
            keep = body.get("attributesToRetrieve")
            h = {k: v for k, v in d.items() if keep is None or k in keep}
            if body.get("attributesToHighlight") or body.get("attributesToCrop"):
                f = {}
                pre, post = body.get("highlightPreTag", "<em>"), body.get("highlightPostTag", "</em>")
                for k, v in d.items():
                    if not isinstance(v, str):
                        continue
                    t = v
                    if k in [c.split(":")[0] for c in body.get("attributesToCrop") or []] and words:
                        pos = t.lower().find(words[0])
                        if pos > 60:
                            t = "…" + t[pos - 40:]
                        t = t[:240]
                    for w in words:
                        t = re.sub(f"(?i)({re.escape(w)})", lambda m: pre + m.group(1) + post, t)
                    f[k] = t
                h["_formatted"] = f
            out.append(h)
        return {"hits": out, **meta}
