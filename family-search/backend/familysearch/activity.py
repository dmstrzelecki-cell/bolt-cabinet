"""Her activity log, for parents. Nothing is recorded unless a parent turns on
Activity monitoring at /parent (the brief: no child search history unless
David enables it). Autocomplete keystrokes are never logged — only searches
she actually submits, tiles she taps, and pages she opens.
"""
from __future__ import annotations

import re
import time
import unicodedata
from typing import Any

from .db import DB, now_iso
from .notify import Notifier

DEFAULT_NOTIFY = {"blocked": True, "watch": True, "request": True, "include_text": False}
RETENTION_CHOICES = (30, 90, 365)
DEFAULT_RETENTION = 90
KINDS = ("search", "tile", "open", "ask", "typed")
DRAFT_WINDOW = 600  # seconds: drafts of the same flagged word merge into one row

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      TEXT NOT NULL,
  kind    TEXT NOT NULL,           -- search | tile | open | ask | typed
  q       TEXT NOT NULL DEFAULT '',
  tab     TEXT NOT NULL DEFAULT '',
  tile    TEXT NOT NULL DEFAULT '',
  results INTEGER,
  url     TEXT NOT NULL DEFAULT '',
  flag    TEXT NOT NULL DEFAULT '',  -- '' | blocked | watch | request
  term    TEXT NOT NULL DEFAULT '',  -- which blocked/watch word matched
  epoch   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_epoch ON activity(epoch);
CREATE INDEX IF NOT EXISTS activity_flag ON activity(flag, id);
"""


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def match_term(q: str, terms: list[str]) -> str | None:
    """The first term that appears in q as whole word(s), or spelled with
    spaces/punctuation squeezed out (e.g. 'p o r n'). None if no match."""
    if not terms or not q:
        return None
    tokens = re.findall(r"[a-z0-9]+", _fold(q))
    padded = " " + " ".join(tokens) + " "
    compact = "".join(tokens)
    for term in terms:
        t = re.findall(r"[a-z0-9]+", _fold(term))
        if not t:
            continue
        if f" {' '.join(t)} " in padded or (compact and compact == "".join(t)):
            return term
    return None


class ActivityService:
    def __init__(self, db: DB, notifier: Notifier):
        self.db = db
        self.notifier = notifier
        with db.conn() as c:
            c.executescript(SCHEMA)
        self._last_prune = 0.0

    # -- settings ----------------------------------------------------------
    def enabled(self) -> bool:
        return bool(self.db.get_setting("store_history", False))

    def notify_opts(self) -> dict[str, bool]:
        return {**DEFAULT_NOTIFY, **(self.db.get_setting("notify") or {})}

    def retention_days(self) -> int:
        d = self.db.get_setting("retention_days", DEFAULT_RETENTION)
        return d if d in RETENTION_CHOICES else DEFAULT_RETENTION

    def show_notice(self) -> bool:
        return bool(self.db.get_setting("show_notice", True))

    # -- writing -----------------------------------------------------------
    def record(self, kind: str, q: str = "", tab: str = "", tile: str = "", results: int | None = None,
               url: str = "", flag: str = "", term: str = "") -> None:
        if kind not in KINDS or not self.enabled():
            return
        superseded = False
        with self.db.conn() as c:
            if flag in ("blocked", "watch") and kind == "search":
                # She went on to search it: the search row replaces the draft row
                # (and was already notified about, so don't notify twice).
                cur = c.execute("DELETE FROM activity WHERE kind='typed' AND term=? AND epoch >= ?",
                                (term[:60], time.time() - DRAFT_WINDOW))
                superseded = cur.rowcount > 0
            c.execute("""INSERT INTO activity(ts, kind, q, tab, tile, results, url, flag, term, epoch)
                         VALUES (?,?,?,?,?,?,?,?,?,?)""",
                      (now_iso(), kind, q[:200], tab[:12], tile[:24], results, url[:500],
                       flag, term[:60], time.time()))
        self._prune()
        opts = self.notify_opts()
        if flag in ("blocked", "watch") and opts.get(flag) and not superseded:
            self.notifier.notify("typed" if kind == "typed" else flag,
                                 q if opts.get("include_text") else None)

    def draft(self, q: str, blocked: list[str], watch: list[str]) -> None:
        """A partly typed query from the autocomplete call. Logged ONLY if it
        contains a blocked or watch word; ordinary typing is never kept.
        Successive drafts of the same word collapse into one row."""
        if not q or not self.enabled():
            return
        flag, term = "blocked", match_term(q, blocked)
        if term is None:
            flag, term = "watch", match_term(q, watch)
        if term is None:
            return
        with self.db.conn() as c:
            row = c.execute("""SELECT id, q FROM activity WHERE kind IN ('typed','search') AND term=?
                               AND epoch >= ? ORDER BY id DESC LIMIT 1""",
                            (term[:60], time.time() - DRAFT_WINDOW)).fetchone()
            if row is not None:
                # Already logged (as a draft or a search) a moment ago: keep the
                # fullest version of the draft, don't add a row or re-notify.
                c.execute("UPDATE activity SET q=?, ts=?, epoch=? WHERE id=? AND kind='typed' "
                          "AND length(?) >= length(q)", (q[:200], now_iso(), time.time(), row["id"], q))
                return
        self.record("typed", q=q, flag=flag, term=term)

    def request_made(self, text: str) -> None:
        """Ask-a-grown-up is saved on her tap regardless; notify if wanted."""
        opts = self.notify_opts()
        if opts.get("request"):
            self.notifier.notify("request", text if opts.get("include_text") else None)
        self.record("ask", q=text, flag="request")

    def _prune(self) -> None:
        now = time.time()
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        with self.db.conn() as c:
            c.execute("DELETE FROM activity WHERE epoch < ?", (now - self.retention_days() * 86400,))

    def clear(self) -> None:
        with self.db.conn() as c:
            c.execute("DELETE FROM activity")

    # -- reading -----------------------------------------------------------
    def rows(self, flagged_only: bool = False, limit: int = 300, before_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM activity WHERE 1=1"
        args: list[Any] = []
        if flagged_only:
            sql += " AND flag != ''"
        if before_id:
            sql += " AND id < ?"
            args.append(before_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(limit, 1000)))
        with self.db.conn() as c:
            return [{k: r[k] for k in r.keys() if k != "epoch"} for r in c.execute(sql, args)]

    def summary(self) -> dict:
        week = time.time() - 7 * 86400
        with self.db.conn() as c:
            total = c.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
            flagged_week = c.execute("SELECT COUNT(*) FROM activity WHERE flag IN ('blocked','watch') "
                                     "AND epoch >= ?", (week,)).fetchone()[0]
            searches_week = c.execute("SELECT COUNT(*) FROM activity WHERE kind='search' AND epoch >= ?",
                                      (week,)).fetchone()[0]
        return {"total": total, "flagged_7d": flagged_week, "searches_7d": searches_week}
