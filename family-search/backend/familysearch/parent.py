"""Parent actions. Every change: validate, write config (with a dated backup),
apply immediately, and write one audit row. No action edits the live index
directly as its source of truth — the index only ever follows config/SQLite.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import (ALBUM_ID_RE, CATEGORY_RE, ConfigError, ConfigStore, Host, _host_from,
                     atomic_write, parse_tiles, validate_tile)
from .db import DB, now_iso
from .meili import WEB, Meili, quote_filter_value
from .photos import PhotoService


class ParentService:
    def __init__(self, cfg: ConfigStore, db: DB, meili: Meili, photos: PhotoService):
        self.cfg = cfg
        self.db = db
        self.meili = meili
        self.photos = photos

    # -- sites -------------------------------------------------------------------
    def site_action(self, actor: str, body: dict) -> dict:
        action = body.get("action")
        with self.cfg.write_lock:
            hosts = dict(self.cfg.hosts())
            if action == "add":
                h = _host_from(body.get("host", ""), _site_fields(body))
                if h.host in hosts:
                    raise ConfigError(f"{h.host} is already on the list.")
                hosts[h.host] = h
                self.cfg.save_hosts(hosts)
                with self.db.tx() as c:
                    c.execute("""INSERT INTO sites(host, added_at, added_by) VALUES (?,?,?)
                                 ON CONFLICT(host) DO UPDATE SET added_at=excluded.added_at,
                                 added_by=excluded.added_by, familylink_confirmed_at=NULL""",
                              (h.host, now_iso(), actor))
                    self.db.audit(actor, "parent", "site added", {"host": h.host, **h.to_yaml()}, c=c)
                if h.crawl:
                    self.request_crawl(actor, [h.host], audit=False)
                return {"host": h.host}

            host = str(body.get("host", "")).strip().lower()
            if host not in hosts:
                raise ConfigError("That host is not on the list.")
            old = hosts[host]
            if action == "update":
                merged = old.to_yaml() | _site_fields(body)
                new = _host_from(host, merged)
                hosts[host] = new
                self.cfg.save_hosts(hosts)
                self.db.audit(actor, "parent", "site updated",
                              {"host": host, "before": old.to_yaml(), "after": new.to_yaml()})
                if (new.boost, new.category) != (old.boost, old.category):
                    self._retag(host, new)
            elif action in ("disable", "enable"):
                hosts[host] = replace(old, enabled=(action == "enable"))
                self.cfg.save_hosts(hosts)
                self.db.audit(actor, "parent", f"site {action}d", {"host": host})
                if action == "disable":
                    # Already hidden: search filters on config. Now drop the docs.
                    self._purge(host)
                elif old.crawl:
                    self.request_crawl(actor, [host], audit=False)
            elif action == "remove":
                del hosts[host]
                self.cfg.save_hosts(hosts)
                with self.db.tx() as c:
                    c.execute("DELETE FROM sites WHERE host=?", (host,))
                    self.db.audit(actor, "parent", "site removed", {"host": host, **old.to_yaml()}, c=c)
                self._purge(host)
            else:
                raise ConfigError("Unknown site action.")
            return {"host": host}

    def _purge(self, host: str) -> None:
        try:
            self.meili.delete_by_filter(WEB, f"host = {quote_filter_value(host)}")
        except Exception:
            pass  # hidden regardless; the crawler also purges on its next run

    def _retag(self, host: str, h: Host) -> None:
        try:
            batch = []
            for d in self.meili.iter_documents(WEB, f"host = {quote_filter_value(host)}", ["id"]):
                batch.append({"id": d["id"], "boost": h.boost, "category": h.category})
                if len(batch) >= 1000:
                    self.meili.update_documents(WEB, batch)
                    batch = []
            self.meili.update_documents(WEB, batch)
        except Exception:
            pass

    def request_crawl(self, actor: str, hosts: list[str] | None, audit: bool = True) -> None:
        approved = self.cfg.approved_hosts()
        hosts = [h for h in (hosts or []) if h in approved] or None
        spool = self.cfg.settings.spool_dir
        req = {"hosts": hosts, "requested_by": actor, "ts": now_iso()}
        existing = spool / "crawl-now"
        if existing.exists():
            try:
                prev = json.loads(existing.read_text())
                if prev.get("hosts") is None or hosts is None:
                    req["hosts"] = None
                else:
                    req["hosts"] = sorted(set(prev["hosts"]) | set(hosts))
            except (ValueError, OSError):
                pass
        atomic_write(existing, json.dumps(req), mode=0o660)
        if audit:
            self.db.audit(actor, "parent", "crawl requested", {"hosts": hosts or "all"})

    def familylink(self, actor: str, host: str, confirmed: bool) -> None:
        host = str(host).strip().lower()
        with self.db.tx() as c:
            c.execute("""INSERT INTO sites(host) VALUES (?) ON CONFLICT(host) DO NOTHING""", (host,))
            c.execute("UPDATE sites SET familylink_confirmed_at=?, familylink_confirmed_by=? WHERE host=?",
                      (now_iso() if confirmed else None, actor if confirmed else None, host))
            self.db.audit(actor, "parent", "family link " + ("confirmed" if confirmed else "unconfirmed"),
                          {"host": host}, c=c)

    # -- albums ------------------------------------------------------------------
    def album_action(self, actor: str, body: dict) -> dict:
        action = body.get("action")
        aid = str(body.get("id", "")).strip().lower()
        if not ALBUM_ID_RE.match(aid):
            raise ConfigError("Album ids look like 8-4-4-4-12 hex (copy it from the Immich album URL).")
        with self.cfg.write_lock:
            albums = [dict(a) for a in self.cfg.albums()]
            ids = [a["id"] for a in albums]
            if action == "add":
                if aid in ids:
                    raise ConfigError("That album is already allowed.")
                albums.append({"id": aid, "name": str(body.get("name", "")).strip()[:80], "enabled": True})
            elif action == "remove":
                if aid not in ids:
                    raise ConfigError("That album is not on the list.")
                albums = [a for a in albums if a["id"] != aid]
            elif action == "rename":
                for a in albums:
                    if a["id"] == aid:
                        a["name"] = str(body.get("name", "")).strip()[:80]
            else:
                raise ConfigError("Unknown album action.")
            self.cfg.save_albums(albums)
        revoked = self.photos.revoke_disabled() if action == "remove" else []
        self.db.audit(actor, "parent", f"album {action}", {"album": aid, "revoked_photos": len(revoked)})
        return {"album": aid, "revoked": len(revoked)}

    # -- requests ----------------------------------------------------------------
    def request_action(self, actor: str, body: dict) -> None:
        action = body.get("action")
        try:
            rid = int(body.get("id"))
        except (TypeError, ValueError):
            raise ConfigError("Bad request id.")
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
            if row is None:
                raise ConfigError("No such request.")
            if action == "delete":
                c.execute("DELETE FROM requests WHERE id=?", (rid,))
            elif action in ("approve", "dismiss"):
                c.execute("UPDATE requests SET status=?, handled_by=?, handled_at=? WHERE id=?",
                          ("approved" if action == "approve" else "dismissed", actor, now_iso(), rid))
            else:
                raise ConfigError("Unknown request action.")
            self.db.audit(actor, "parent", f"request {action}",
                          {"id": rid, "kind": row["kind"], "text": row["text"]}, c=c)

    # -- filters / tiles / settings ---------------------------------------------
    def save_filters(self, actor: str, body: dict) -> None:
        from .config import parse_filters
        import yaml
        doc = parse_filters(yaml.safe_dump({"blocked": body.get("blocked") or [],
                                            "watch": body.get("watch") or [],
                                            "synonyms": body.get("synonyms") or {}}))
        with self.cfg.write_lock:
            before = self.cfg.filters()
            self.cfg.save_filters(doc)
        try:
            self.meili.set_synonyms(doc["synonyms"])
        except Exception:
            pass
        self.db.audit(actor, "parent", "filters saved", {
            "blocked_added": sorted(set(doc["blocked"]) - set(before["blocked"])),
            "blocked_removed": sorted(set(before["blocked"]) - set(doc["blocked"])),
            "watch_added": sorted(set(doc["watch"]) - set(before.get("watch", []))),
            "watch_removed": sorted(set(before.get("watch", [])) - set(doc["watch"])),
            "synonyms": len(doc["synonyms"]),
        })

    def save_tiles(self, actor: str, body: dict) -> None:
        tiles = body.get("tiles")
        if not isinstance(tiles, list) or len(tiles) > 40:
            raise ConfigError("Tiles must be a list (40 at most).")
        doc = {"tiles": [validate_tile(t) for t in tiles],
               "curious_hosts": self.cfg.tiles().get("curious_hosts", [])}
        if "curious_hosts" in body:
            doc["curious_hosts"] = body["curious_hosts"]
        from .config import dump_tiles
        parse_tiles(dump_tiles(doc))  # validates ids unique, hosts, etc.
        with self.cfg.write_lock:
            before = [t["id"] for t in self.cfg.tiles()["tiles"]]
            self.cfg.save_tiles(doc)
        self.db.audit(actor, "parent", "tiles saved",
                      {"before": before, "after": [t["id"] + ("(hidden)" if t["hidden"] else "")
                                                   for t in doc["tiles"]]})

    def save_settings(self, actor: str, body: dict) -> None:
        from .activity import DEFAULT_NOTIFY, RETENTION_CHOICES
        if "store_history" in body:
            val = bool(body["store_history"])
            self.db.set_setting("store_history", val)
            self.db.audit(actor, "parent", "activity monitoring " + ("on" if val else "off"))
        if "notify" in body:
            raw = body["notify"] if isinstance(body["notify"], dict) else {}
            opts = {k: bool(raw.get(k, v)) for k, v in DEFAULT_NOTIFY.items()}
            self.db.set_setting("notify", opts)
            self.db.audit(actor, "parent", "notification settings saved", opts)
        if "retention_days" in body:
            try:
                days = int(body["retention_days"])
            except (TypeError, ValueError):
                days = 0
            if days not in RETENTION_CHOICES:
                raise ConfigError(f"Keep the log for {', '.join(map(str, RETENTION_CHOICES))} days.")
            self.db.set_setting("retention_days", days)
            self.db.audit(actor, "parent", "activity log retention", {"days": days})
        if "watch_prompt" in body:
            val = bool(body["watch_prompt"])
            self.db.set_setting("watch_prompt", val)
            self.db.audit(actor, "parent", "ask-a-grown-up on watch words " + ("on" if val else "off"))
        if "show_notice" in body:
            val = bool(body["show_notice"])
            self.db.set_setting("show_notice", val)
            self.db.audit(actor, "parent", "monitoring notice " + ("shown" if val else "hidden"))

    def _settings(self) -> dict:
        from .activity import DEFAULT_NOTIFY, DEFAULT_RETENTION
        return {
            "store_history": bool(self.db.get_setting("store_history", False)),
            "notify": {**DEFAULT_NOTIFY, **(self.db.get_setting("notify") or {})},
            "retention_days": self.db.get_setting("retention_days", DEFAULT_RETENTION),
            "show_notice": bool(self.db.get_setting("show_notice", True)),
            "watch_prompt": bool(self.db.get_setting("watch_prompt", True)),
        }

    # -- read model --------------------------------------------------------------
    def state(self, team_domain: str | None, public_host: str, library_rows: list[dict]) -> dict:
        hosts = self.cfg.hosts()
        crawl_status = self._crawl_status()
        with self.db.conn() as c:
            site_rows = {r["host"]: dict(r) for r in c.execute("SELECT * FROM sites")}
            albums_state = {r["album_id"]: dict(r) for r in c.execute("SELECT * FROM albums_state")}
            reqs = [dict(r) for r in c.execute(
                "SELECT * FROM requests ORDER BY CASE status WHEN 'new' THEN 0 ELSE 1 END, id DESC LIMIT 200")]
        sites = []
        for h in sorted(hosts.values(), key=lambda x: x.host):
            st = crawl_status.get("hosts", {}).get(h.host, {})
            sr = site_rows.get(h.host, {})
            sites.append({**h.to_yaml(), "host": h.host, "enabled": h.enabled, "crawl": h.crawl,
                          "page_cap": h.page_cap, "boost": h.boost, "name": h.name,
                          "familylink_confirmed": bool(sr.get("familylink_confirmed_at")),
                          "crawl_status": st})
        # Family Link needs every host the phone will actually open.
        fl_hosts = [public_host] + ([team_domain] if team_domain else [])
        fl_hosts += [h.host for h in hosts.values() if h.enabled]
        fl_hosts += sorted({r["host"] for r in library_rows if r.get("host")})
        seen, fl = set(), []
        for h in fl_hosts:
            if h and h not in seen:
                seen.add(h)
                confirmed = bool(site_rows.get(h, {}).get("familylink_confirmed_at"))
                fl.append({"host": h, "confirmed": confirmed})
        albums = []
        for a in self.cfg.albums():
            s = albums_state.get(a["id"], {})
            albums.append({**a, "photo_count": s.get("photo_count", 0),
                           "last_sync": s.get("last_sync"), "last_error": s.get("last_error"),
                           "immich_name": s.get("name")})
        return {
            "sites": sites,
            "categories": sorted({h.category for h in hosts.values()}),
            "crawl": {k: v for k, v in crawl_status.items() if k != "hosts"},
            "crawl_pending": (self.cfg.settings.spool_dir / "crawl-now").exists(),
            "familylink": fl,
            "albums": albums,
            "requests": reqs,
            "filters": self.cfg.filters(),
            "tiles": self.cfg.tiles(),
            "settings": self._settings(),
        }

    def _crawl_status(self) -> dict:
        p: Path = self.cfg.settings.crawl_status_path
        try:
            st = json.loads(p.read_text())
        except (OSError, ValueError):
            return {}
        self._ingest_run(st.get("last_run"))
        return st

    def ingest_crawl_status(self) -> None:
        self._crawl_status()

    def _ingest_run(self, run: dict | None) -> None:
        """Copy each finished crawl run into the audit log exactly once."""
        if not isinstance(run, dict) or not run.get("finished"):
            return
        if self.db.get_setting("last_crawl_audited") == run["finished"]:
            return
        self.db.set_setting("last_crawl_audited", run["finished"])
        self.db.audit("crawler", "crawl", "crawl finished", {
            "started": run.get("started"), "finished": run.get("finished"),
            **{k: v for k, v in (run.get("totals") or {}).items() if isinstance(v, int)},
            "needs_review": run.get("problems") or [],
        })


def _site_fields(body: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in ("category", "page_cap", "boost", "crawl", "name", "schedule_day"):
        if k in body and body[k] is not None and body[k] != "":
            out[k] = body[k]
    if "category" in out:
        out["category"] = str(out["category"]).strip().lower()
        if not CATEGORY_RE.match(out["category"]):
            raise ConfigError("Categories are lowercase words and dashes, e.g. animals-science.")
    return out

