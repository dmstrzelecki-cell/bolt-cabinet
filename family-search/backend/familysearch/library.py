"""Parent-curated library: single approved URLs, written notes, and PDFs.

PDFs live outside the web root (data_dir/app/library/<id>.pdf) and are served
only through /api/library/<id>/file with a sandboxing CSP.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from .config import ConfigError, validate_host
from .db import DB, now_iso
from .meili import LIBRARY, Meili
from .search import clean_text, safe_https_url

MAX_PDF = 25 * 1024 * 1024
MAX_NOTE = 5000


def _doc(row) -> dict:
    return {"id": row["id"], "kind": row["kind"], "title": row["title"],
            "note": row["note"], "body": row["note"] if row["kind"] == "note" else ""}


class LibraryService:
    def __init__(self, db: DB, meili: Meili, files_dir: Path):
        self.db = db
        self.meili = meili
        self.files_dir = files_dir

    def _insert(self, actor: str, kind: str, title: str, note: str, url: str | None = None,
                host: str | None = None, file_name: str | None = None, size: int | None = None) -> dict:
        lid = secrets.token_urlsafe(12)
        with self.db.tx() as c:
            c.execute("""INSERT INTO library(id, kind, title, note, url, host, file_name, file_size,
                         created, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                      (lid, kind, title, note, url, host, file_name, size, now_iso(), actor))
            self.db.audit(actor, "parent", f"library: added {kind}",
                          {"id": lid, "title": title, "host": host}, c=c)
            row = c.execute("SELECT * FROM library WHERE id=?", (lid,)).fetchone()
        try:
            self.meili.add_documents(LIBRARY, [_doc(row)])
        except Exception:
            pass  # rebuild-index.sh re-pushes from SQLite
        return dict(row)

    @staticmethod
    def _title_note(title: str, note: str) -> tuple[str, str]:
        title = clean_text(title, 120)
        note = clean_text(note, MAX_NOTE)
        return title, note

    def add_url(self, actor: str, url: str, title: str, note: str = "") -> dict:
        url = (url or "").strip()
        if not safe_https_url(url):
            raise ConfigError("Only https:// links can be added.")
        host = validate_host(urlsplit(url).hostname or "")
        title, note = self._title_note(title or host, note)
        return self._insert(actor, "url", title, note, url=url, host=host)

    def add_note(self, actor: str, title: str, note: str) -> dict:
        title, note = self._title_note(title, note)
        if not title or not note:
            raise ConfigError("A note needs a title and some text.")
        return self._insert(actor, "note", title, note)

    def add_pdf(self, actor: str, title: str, note: str, data: bytes) -> dict:
        if len(data) > MAX_PDF:
            raise ConfigError("PDFs can be up to 25 MB.")
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
            raise ConfigError("That file is not a PDF.")
        title, note = self._title_note(title, note)
        if not title:
            raise ConfigError("A PDF needs a title.")
        self.files_dir.mkdir(parents=True, exist_ok=True)
        name = f"{secrets.token_hex(16)}.pdf"
        path = self.files_dir / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            return self._insert(actor, "pdf", title, note, file_name=name, size=len(data))
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def remove(self, actor: str, lid: str) -> None:
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM library WHERE id=?", (lid,)).fetchone()
            if row is None:
                raise ConfigError("No such library item.")
            c.execute("DELETE FROM library WHERE id=?", (lid,))
            self.db.audit(actor, "parent", "library: removed",
                          {"id": lid, "title": row["title"], "kind": row["kind"]}, c=c)
        if row["file_name"]:
            (self.files_dir / row["file_name"]).unlink(missing_ok=True)
        try:
            self.meili.delete_documents(LIBRARY, [lid])
        except Exception:
            pass

    def get(self, lid: str):
        with self.db.conn() as c:
            return c.execute("SELECT * FROM library WHERE id=? AND enabled=1", (lid,)).fetchone()

    def all(self) -> list[dict]:
        with self.db.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM library ORDER BY created DESC")]

    def pdf_path(self, row) -> Path | None:
        if row is None or row["kind"] != "pdf" or not row["file_name"]:
            return None
        p = (self.files_dir / row["file_name"]).resolve()
        if p.parent != self.files_dir.resolve() or not p.is_file():
            return None
        return p

    def reindex(self) -> int:
        with self.db.conn() as c:
            rows = c.execute("SELECT * FROM library WHERE enabled=1").fetchall()
        docs = [_doc(r) for r in rows]
        for i in range(0, len(docs), 1000):
            self.meili.add_documents(LIBRARY, docs[i:i + 1000])
        return len(docs)
