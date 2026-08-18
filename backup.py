"""Database (and appeal photo) backups.

The SQLite database holds almost all persistent state -- accounts,
manifests, scans, billing, audit log, everything except one thing: worker
appeal photos, which live on disk at data/appeal_photos/ with only their
path referenced from exceptions.photo_path (see app.py's APPEAL_PHOTOS_DIR
and /w/appeal). "Backup of everything" therefore means two paired
artifacts per backup round, not one -- this module produces both, tagged
with the same timestamp so they're restored together as one snapshot.

Backups always land in backup_data/, never inside data/ itself. The
database half is taken with SQLite's online backup API
(`sqlite3.Connection.backup()`), safe to run against a live database in
WAL mode with other connections actively reading/writing -- no need to
stop the server first. The photos half is a plain zip of whatever's in
appeal_photos/ at that moment (photo files are write-once after upload,
so there's no equivalent "hot backup" concern there).
"""

from __future__ import annotations

import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

import db

BASE_DIR = Path(__file__).parent
DEFAULT_BACKUP_DIR = BASE_DIR / "backup_data"
DEFAULT_RETENTION = 30


def _make_timestamp() -> str:
    """UTC timestamp for backup filenames.

    UTC rather than server-local for the same reason everything else in
    this system stores UTC: filenames are what retention sorts on, and a
    local clock that jumps backwards an hour at the end of DST produces
    two backups an hour apart whose names sort in the wrong order. The
    pruner would then delete the newer one.

    Backups taken before this changed carry local-time names. They still
    sort correctly among themselves; only the ordering across the
    changeover is approximate, and only by the host's UTC offset.
    """
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _unique_path(backup_dir: Path, stem: str, timestamp: str, suffix: str) -> Path:
    dest = backup_dir / f"{stem}-{timestamp}{suffix}"
    n = 1
    while dest.exists():
        dest = backup_dir / f"{stem}-{timestamp}-{n}{suffix}"
        n += 1
    return dest


def backup_database(
    db_path: Path | str | None = None,
    backup_dir: Path | str | None = None,
    retention: int = DEFAULT_RETENTION,
    timestamp: str | None = None,
) -> Path:
    """Hot-copy db_path into backup_dir with a timestamped filename, then
    enforce retention (oldest backups beyond `retention` are deleted).
    Returns the path to the new backup file.

    `timestamp` lets backup_everything() pair this with a photos backup
    taken in the same round; omit it to generate one now.
    """
    db_path = Path(db_path) if db_path else db.DEFAULT_DB_PATH
    backup_dir = Path(backup_dir) if backup_dir else DEFAULT_BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)

    if not db_path.exists():
        raise FileNotFoundError(f"no database at {db_path} to back up")

    dest_path = _unique_path(backup_dir, db_path.stem, timestamp or _make_timestamp(), ".db")

    source = sqlite3.connect(str(db_path))
    dest = sqlite3.connect(str(dest_path))
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()

    _enforce_retention(backup_dir, f"{db_path.stem}-*.db", retention)
    return dest_path


def backup_appeal_photos(
    photos_dir: Path | str,
    backup_dir: Path | str | None = None,
    retention: int = DEFAULT_RETENTION,
    timestamp: str | None = None,
) -> Path | None:
    """Zip whatever's in photos_dir into backup_dir. Returns None (not an
    error) if there are no photos yet -- nothing to back up isn't a
    failure."""
    photos_dir = Path(photos_dir)
    backup_dir = Path(backup_dir) if backup_dir else DEFAULT_BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)

    if not photos_dir.exists() or not any(p.is_file() for p in photos_dir.iterdir()):
        return None

    dest_path = _unique_path(backup_dir, "appeal_photos", timestamp or _make_timestamp(), ".zip")
    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(photos_dir.iterdir()):
            if file.is_file():
                zf.write(file, arcname=file.name)

    _enforce_retention(backup_dir, "appeal_photos-*.zip", retention)
    return dest_path


class BackupResult(TypedDict):
    db: Path
    photos: Path | None


def backup_everything(
    db_path: Path | str | None = None,
    photos_dir: Path | str | None = None,
    backup_dir: Path | str | None = None,
    retention: int = DEFAULT_RETENTION,
) -> BackupResult:
    """Back up the database and appeal photos as one paired snapshot
    (same timestamp on both filenames). This is what serve.py and the
    operator GUI actually call -- backup_database()/backup_appeal_photos()
    are exposed separately mainly for testing and for the rare case you
    only want one half."""
    db_path = Path(db_path) if db_path else db.DEFAULT_DB_PATH
    photos_dir = Path(photos_dir) if photos_dir else db_path.parent / "appeal_photos"
    backup_dir = Path(backup_dir) if backup_dir else DEFAULT_BACKUP_DIR
    timestamp = _make_timestamp()

    db_backup = backup_database(db_path, backup_dir, retention=retention, timestamp=timestamp)
    photos_backup = backup_appeal_photos(
        photos_dir, backup_dir, retention=retention, timestamp=timestamp
    )
    return {"db": db_backup, "photos": photos_backup}


def _enforce_retention(backup_dir: Path, glob_pattern: str, retention: int) -> None:
    if retention <= 0:
        return
    backups = sorted(backup_dir.glob(glob_pattern))
    excess = len(backups) - retention
    for old in backups[: max(excess, 0)]:
        old.unlink()


def list_backups(backup_dir: Path | str | None = None, stem: str | None = None) -> list[Path]:
    backup_dir = Path(backup_dir) if backup_dir else DEFAULT_BACKUP_DIR
    if not backup_dir.exists():
        return []
    pattern = f"{stem}-*.db" if stem else "*.db"
    return sorted(backup_dir.glob(pattern), reverse=True)


def list_photo_backups(backup_dir: Path | str | None = None) -> list[Path]:
    backup_dir = Path(backup_dir) if backup_dir else DEFAULT_BACKUP_DIR
    if not backup_dir.exists():
        return []
    return sorted(backup_dir.glob("appeal_photos-*.zip"), reverse=True)


def restore_backup(backup_path: Path | str, db_path: Path | str | None = None) -> None:
    """Restore a database backup file over the live database. The caller
    is responsible for making sure no other process holds the database
    open when this runs -- this does not stop the server."""
    backup_path = Path(backup_path)
    db_path = Path(db_path) if db_path else db.DEFAULT_DB_PATH
    if not backup_path.exists():
        raise FileNotFoundError(f"no backup at {backup_path}")

    source = sqlite3.connect(str(backup_path))
    dest = sqlite3.connect(str(db_path))
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()


def restore_appeal_photos(photos_backup_path: Path | str, photos_dir: Path | str) -> None:
    """Extract a photos zip backup over photos_dir. Existing files with
    the same name are overwritten; files added since the backup was taken
    are left alone (this restores photos, it doesn't delete newer ones)."""
    photos_backup_path = Path(photos_backup_path)
    photos_dir = Path(photos_dir)
    if not photos_backup_path.exists():
        raise FileNotFoundError(f"no photo backup at {photos_backup_path}")
    photos_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(photos_backup_path, "r") as zf:
        zf.extractall(photos_dir)
