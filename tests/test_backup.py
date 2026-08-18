import time

import backup
import db
import seed


def test_backup_creates_a_working_copy_with_same_data(tmp_path):
    db_path = tmp_path / "app.db"
    backup_dir = tmp_path / "backup_data"
    conn = db.init_db(db_path)
    info = seed.ensure_seeded(conn)
    conn.close()

    dest = backup.backup_database(db_path, backup_dir)
    assert dest.exists()
    assert dest.parent == backup_dir

    restored_conn = db.connect(dest)
    account = db.get_account(restored_conn, info["account_id"])
    assert account["name"] == "Dockside Supply Co. (demo)"
    manifests = db.list_manifests(restored_conn, info["account_id"])
    assert len(manifests) == 3
    restored_conn.close()


def test_backup_does_not_write_into_the_data_directory(tmp_path):
    db_path = tmp_path / "data" / "app.db"
    backup_dir = tmp_path / "backup_data"
    db.init_db(db_path).close()

    dest = backup.backup_database(db_path, backup_dir)
    assert dest.parent == backup_dir
    assert dest.parent != db_path.parent


def test_backup_missing_database_raises(tmp_path):
    try:
        backup.backup_database(tmp_path / "nope.db", tmp_path / "backup_data")
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError:
        pass


def test_backup_retention_deletes_oldest_beyond_limit(tmp_path):
    db_path = tmp_path / "app.db"
    backup_dir = tmp_path / "backup_data"
    db.init_db(db_path).close()

    made = []
    for _ in range(5):
        made.append(backup.backup_database(db_path, backup_dir, retention=3))
        time.sleep(1.05)  # filenames are second-resolution; force distinct timestamps

    remaining = backup.list_backups(backup_dir, stem="app")
    assert len(remaining) == 3
    # The three most recent backups (the last three made) should survive.
    remaining_names = {p.name for p in remaining}
    for p in made[-3:]:
        assert p.name in remaining_names
    for p in made[:-3]:
        assert p.name not in remaining_names


def test_restore_backup_overwrites_live_database(tmp_path):
    db_path = tmp_path / "app.db"
    backup_dir = tmp_path / "backup_data"
    conn = db.init_db(db_path)
    seed.ensure_seeded(conn)
    conn.close()

    dest = backup.backup_database(db_path, backup_dir)

    # Mutate the live DB after the backup was taken.
    conn = db.connect(db_path)
    db.execute(conn, "UPDATE accounts SET name = 'Mutated' WHERE id = 1")
    conn.close()

    backup.restore_backup(dest, db_path)

    conn = db.connect(db_path)
    account = db.get_account(conn, 1)
    assert account["name"] == "Dockside Supply Co. (demo)"
    conn.close()


def test_backup_appeal_photos_returns_none_when_directory_empty(tmp_path):
    photos_dir = tmp_path / "appeal_photos"
    backup_dir = tmp_path / "backup_data"
    assert backup.backup_appeal_photos(photos_dir, backup_dir) is None
    photos_dir.mkdir()
    assert backup.backup_appeal_photos(photos_dir, backup_dir) is None


def test_backup_appeal_photos_zips_existing_files(tmp_path):
    photos_dir = tmp_path / "appeal_photos"
    photos_dir.mkdir()
    (photos_dir / "abc123.jpg").write_bytes(b"\xff\xd8\xff\xe0FAKE")
    (photos_dir / "def456.jpg").write_bytes(b"\xff\xd8\xff\xe0OTHER")
    backup_dir = tmp_path / "backup_data"

    dest = backup.backup_appeal_photos(photos_dir, backup_dir)
    assert dest is not None
    assert dest.parent == backup_dir
    assert dest.suffix == ".zip"

    import zipfile

    with zipfile.ZipFile(dest) as zf:
        names = set(zf.namelist())
    assert names == {"abc123.jpg", "def456.jpg"}


def test_backup_everything_pairs_db_and_photos_with_same_timestamp(tmp_path):
    db_path = tmp_path / "app.db"
    photos_dir = tmp_path / "appeal_photos"
    backup_dir = tmp_path / "backup_data"
    db.init_db(db_path).close()
    photos_dir.mkdir()
    (photos_dir / "x.jpg").write_bytes(b"\xff\xd8")

    result = backup.backup_everything(db_path, photos_dir, backup_dir)
    assert result["db"].exists()
    assert result["photos"].exists()
    db_timestamp = result["db"].stem.split("-", 1)[1]
    photos_timestamp = result["photos"].stem.split("-", 1)[1]
    assert db_timestamp == photos_timestamp


def test_backup_everything_photos_is_none_when_no_appeals_yet(tmp_path):
    db_path = tmp_path / "app.db"
    backup_dir = tmp_path / "backup_data"
    db.init_db(db_path).close()
    result = backup.backup_everything(db_path, tmp_path / "appeal_photos", backup_dir)
    assert result["db"].exists()
    assert result["photos"] is None


def test_restore_appeal_photos_extracts_files(tmp_path):
    photos_dir = tmp_path / "appeal_photos"
    photos_dir.mkdir()
    (photos_dir / "keep.jpg").write_bytes(b"\xff\xd8\xff\xe0ORIGINAL")
    backup_dir = tmp_path / "backup_data"
    dest = backup.backup_appeal_photos(photos_dir, backup_dir)

    # Simulate data loss, then restore.
    (photos_dir / "keep.jpg").unlink()
    assert not (photos_dir / "keep.jpg").exists()
    backup.restore_appeal_photos(dest, photos_dir)
    assert (photos_dir / "keep.jpg").read_bytes() == b"\xff\xd8\xff\xe0ORIGINAL"


def test_restore_appeal_photos_does_not_delete_newer_files(tmp_path):
    photos_dir = tmp_path / "appeal_photos"
    photos_dir.mkdir()
    (photos_dir / "old.jpg").write_bytes(b"OLD")
    backup_dir = tmp_path / "backup_data"
    dest = backup.backup_appeal_photos(photos_dir, backup_dir)

    (photos_dir / "new.jpg").write_bytes(b"NEW")  # added after the backup
    backup.restore_appeal_photos(dest, photos_dir)
    assert (photos_dir / "old.jpg").exists()
    assert (photos_dir / "new.jpg").exists()  # not wiped out by restore


def test_photo_backup_retention_deletes_oldest_beyond_limit(tmp_path):
    photos_dir = tmp_path / "appeal_photos"
    photos_dir.mkdir()
    (photos_dir / "p.jpg").write_bytes(b"\xff\xd8")
    backup_dir = tmp_path / "backup_data"

    made = []
    for _ in range(5):
        made.append(backup.backup_appeal_photos(photos_dir, backup_dir, retention=3))
        time.sleep(1.05)

    remaining = backup.list_photo_backups(backup_dir)
    assert len(remaining) == 3
    remaining_names = {p.name for p in remaining}
    for p in made[-3:]:
        assert p.name in remaining_names


def test_list_backups_orders_newest_first(tmp_path):
    db_path = tmp_path / "app.db"
    backup_dir = tmp_path / "backup_data"
    db.init_db(db_path).close()

    first = backup.backup_database(db_path, backup_dir)
    time.sleep(1.05)
    second = backup.backup_database(db_path, backup_dir)

    backups = backup.list_backups(backup_dir, stem="app")
    assert backups[0] == second
    assert backups[1] == first
