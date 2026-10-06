"""Backup and restore of everything BatteryAI keeps in /data: the database (readings,
predictions, monthly bill), the settings (sensors, tariffs, programs, API keys) and the
control state. The local LLM model is left out; it can be downloaded again."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from config import DATA_DIR
from db import Database

DB_NAME = "batteryai.db"
MANIFEST = "manifest.json"
WORK_DIR = Path(DATA_DIR) / "backup-tmp"
# Never part of a backup: the model, partial downloads, SQLite side files, temp folders.
SKIP_SUFFIXES = (".gguf", ".part", "-wal", "-shm", ".tmp", ".before-restore")


class BackupError(ValueError):
    pass


def _extra_files() -> list[Path]:
    """Small files next to the database (settings.json, control.json, …)."""
    data = Path(DATA_DIR)
    if not data.exists():
        return []
    return sorted(
        item for item in data.iterdir()
        if item.is_file() and item.name != DB_NAME and not item.name.startswith(DB_NAME) and not item.name.endswith(SKIP_SUFFIXES)
    )


def create_backup(db: Database, version: str) -> Path:
    """Writes a zip with the database and settings; returns its path (in WORK_DIR)."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    snapshot = WORK_DIR / f"snapshot-{stamp}.db"
    target = WORK_DIR / f"batteryai-backup-{stamp}.zip"
    try:
        db.backup_to(str(snapshot))
        files = _extra_files()
        manifest = {
            "app": "batteryai",
            "version": version,
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "files": [DB_NAME, *(f.name for f in files)],
        }
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            archive.writestr(MANIFEST, json.dumps(manifest, indent=2))
            archive.write(snapshot, DB_NAME)
            for item in files:
                archive.write(item, item.name)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        snapshot.unlink(missing_ok=True)
    return target


def check_backup(path: Path) -> dict[str, Any]:
    """Validates an uploaded backup and unpacks it into WORK_DIR/restore; returns its manifest."""
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as err:
        raise BackupError("This is not a BatteryAI backup (not a zip file).") from err
    with archive:
        names = set(archive.namelist())
        if MANIFEST not in names or DB_NAME not in names:
            raise BackupError("This is not a BatteryAI backup (manifest or database missing).")
        manifest = json.loads(archive.read(MANIFEST))
        if manifest.get("app") != "batteryai":
            raise BackupError("This backup was not made by BatteryAI.")
        out = WORK_DIR / "restore"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        for name in names:
            # Only plain file names: nothing may be written outside the restore folder.
            if name != os.path.basename(name) or name in (".", "..") or name.endswith(SKIP_SUFFIXES):
                continue
            with archive.open(name) as src, open(out / name, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
    conn = sqlite3.connect(out / DB_NAME)
    try:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise BackupError("The database in the backup is damaged.")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "readings" not in tables:
            raise BackupError("The database in the backup has no readings table.")
        manifest["readings"] = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    except sqlite3.DatabaseError as err:
        raise BackupError(f"The database in the backup cannot be read: {err}") from err
    finally:
        conn.close()
    for name in ("settings.json", "control.json"):
        if (out / name).exists():
            try:
                json.loads((out / name).read_text(encoding="utf-8"))
            except ValueError as err:
                raise BackupError(f"{name} in the backup is not valid JSON.") from err
    return manifest


def install_restore() -> None:
    """Moves the unpacked backup into /data. The database must be closed. The current
    database is kept as batteryai.db.before-restore until the next restore."""
    data = Path(DATA_DIR)
    source = WORK_DIR / "restore"
    current = data / DB_NAME
    if current.exists():
        os.replace(current, data / (DB_NAME + ".before-restore"))
    for suffix in ("-wal", "-shm"):
        (data / (DB_NAME + suffix)).unlink(missing_ok=True)
    for item in source.iterdir():
        if item.name != MANIFEST:
            os.replace(item, data / item.name)
    shutil.rmtree(source, ignore_errors=True)


def cleanup(older_than: float = 3600) -> None:
    """Removes leftover backup files from WORK_DIR."""
    if not WORK_DIR.exists():
        return
    for item in WORK_DIR.iterdir():
        if item.is_file() and time.time() - item.stat().st_mtime > older_than:
            item.unlink(missing_ok=True)
