"""Acceptance: restoring the backup restores sites, tiles, library entries and
settings (runs the real backup.sh / restore.sh against temp directories)."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI not installed")


def test_backup_then_restore_round_trip(env, parent, core, tmp_path):
    # state A: a site, a tile change, a library note, history on
    parent.post("/api/parent/sites", json={"action": "add", "host": "www.example.org", "category": "reference"})
    st = parent.get("/api/parent/state").json()
    tiles = st["tiles"]["tiles"][:3]
    parent.post("/api/parent/tiles", json={"tiles": tiles})
    parent.post("/api/parent/library", json={"action": "add_note", "title": "Bedtime", "note": "8pm"})
    parent.post("/api/parent/settings", json={"store_history": True})

    fake_root = tmp_path / "opt"
    fake_root.mkdir()
    shutil.copytree(core.s.config_dir, fake_root / "config")  # backup reads $FS_ROOT/config
    envs = {**os.environ, "FS_ROOT": str(fake_root), "FS_DATA_DIR": str(core.s.data_dir),
            "FS_ETC": str(tmp_path / "etc"), "FS_BACKUP_DIR": str(tmp_path / "backups"), "FS_SYSTEMD": "0"}
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "backend.env").write_text("CSRF_SECRET=x\n")
    subprocess.run(["bash", str(ROOT / "scripts/backup.sh")], env=envs, check=True, capture_output=True)
    archive = next((tmp_path / "backups").glob("family-search-*.tar.gz"))

    # state B: everything changed / removed
    parent.post("/api/parent/sites", json={"action": "remove", "host": "www.example.org"})
    parent.post("/api/parent/tiles", json={"tiles": st["tiles"]["tiles"]})
    lid = parent.get("/api/parent/state").json()["library"][0]["id"]
    parent.post("/api/parent/library", json={"action": "remove", "id": lid})
    parent.post("/api/parent/settings", json={"store_history": False})

    shutil.rmtree(fake_root / "config")
    shutil.copytree(core.s.config_dir, fake_root / "config")
    r = subprocess.run(["bash", str(ROOT / "scripts/restore.sh"), str(archive)], env=envs,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    shutil.rmtree(core.s.config_dir)
    shutil.copytree(fake_root / "config", core.s.config_dir)

    st2 = parent.get("/api/parent/state").json()
    assert "www.example.org" in {s["host"] for s in st2["sites"]}
    assert [t["id"] for t in st2["tiles"]["tiles"]] == [t["id"] for t in tiles]
    assert [x["title"] for x in st2["library"]] == ["Bedtime"]
    assert st2["settings"]["store_history"] is True
    assert len(list((tmp_path / "backups").glob("family-search-*pre-restore.tar.gz"))) == 1
