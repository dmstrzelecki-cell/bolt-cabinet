"""SQLite: library entries, requests, opaque photo ids, site bookkeeping, audit.

The search index is never the source of truth. Everything needed to rebuild
it lives here, in config/, and in the crawler's own page store.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS audit (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      TEXT NOT NULL,
  actor   TEXT NOT NULL,
  kind    TEXT NOT NULL,          -- parent | crawl | sync | denied | system
  action  TEXT NOT NULL,
  detail  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);

CREATE TABLE IF NOT EXISTS sites (
  host                   TEXT PRIMARY KEY,
  added_at               TEXT,
  added_by               TEXT,
  familylink_confirmed_at TEXT,
  familylink_confirmed_by TEXT
);

CREATE TABLE IF NOT EXISTS library (
  id         TEXT PRIMARY KEY,
  kind       TEXT NOT NULL CHECK (kind IN ('url','note','pdf')),
  title      TEXT NOT NULL,
  note       TEXT NOT NULL DEFAULT '',
  url        TEXT,
  host       TEXT,
  file_name  TEXT,
  file_size  INTEGER,
  enabled    INTEGER NOT NULL DEFAULT 1,
  created    TEXT NOT NULL,
  created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  kind       TEXT NOT NULL CHECK (kind IN ('query','site')),
  text       TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'new',
  handled_by TEXT,
  handled_at TEXT
);

CREATE TABLE IF NOT EXISTS photos (
  opaque_id   TEXT PRIMARY KEY,
  immich_id   TEXT NOT NULL,
  album_id    TEXT NOT NULL,
  taken_date  TEXT,
  description TEXT NOT NULL DEFAULT '',
  synced_at   TEXT NOT NULL,
  UNIQUE (immich_id, album_id)
);
CREATE INDEX IF NOT EXISTS photos_album ON photos(album_id);

CREATE TABLE IF NOT EXISTS albums_state (
  album_id    TEXT PRIMARY KEY,
  name        TEXT,
  photo_count INTEGER NOT NULL DEFAULT 0,
  last_sync   TEXT,
  last_error  TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- The activity log table is created by activity.py.
"""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class DB:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=10000")
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    # -- audit ------------------------------------------------------------
    def audit(self, actor: str, kind: str, action: str, detail: dict[str, Any] | None = None,
              c: sqlite3.Connection | None = None) -> None:
        row = (now_iso(), actor, kind, action, json.dumps(detail or {}, sort_keys=True)[:4000])
        sql = "INSERT INTO audit(ts, actor, kind, action, detail) VALUES (?,?,?,?,?)"
        if c is not None:
            c.execute(sql, row)
        else:
            with self.conn() as c2:
                c2.execute(sql, row)

    def audit_rows(self, limit: int = 200, kind: str | None = None) -> list[dict]:
        with self.conn() as c:
            if kind:
                rows = c.execute("SELECT * FROM audit WHERE kind=? ORDER BY id DESC LIMIT ?",
                                 (kind, limit)).fetchall()
            else:
                rows = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d["detail"])
            except ValueError:
                d["detail"] = {}
            out.append(d)
        return out

    # -- settings ---------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.conn() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set_setting(self, key: str, value: Any, c: sqlite3.Connection | None = None) -> None:
        sql = "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        if c is not None:
            c.execute(sql, (key, json.dumps(value)))
        else:
            with self.conn() as c2:
                c2.execute(sql, (key, json.dumps(value)))
