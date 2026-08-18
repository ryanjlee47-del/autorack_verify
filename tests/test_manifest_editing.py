import db
import manifest_ingest


def _seeded_manifest(conn, n=5, loose_match_enabled=True):
    account_id = db.create_account(
        conn, "A", 900, 25, loose_match_enabled=loose_match_enabled, loose_suffix_len=8
    )
    rows = [
        {
            "line_no": i + 1,
            "sku": f"S{i}",
            "description": f"D{i}",
            "qty_expected": 1,
            "raw_barcode": f"{1000 + i}",
        }
        for i in range(n)
    ]
    manifest_id, _report = manifest_ingest.commit_manifest(
        conn, account_id, "M1", None, rows, loose_match_enabled, 8
    )
    return account_id, manifest_id


def test_get_manifest_lines_page_paginates_in_sql(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    _account_id, manifest_id = _seeded_manifest(conn, n=10)

    page1, total = db.get_manifest_lines_page(conn, manifest_id, limit=4, offset=0)
    page2, _ = db.get_manifest_lines_page(conn, manifest_id, limit=4, offset=4)
    assert total == 10
    assert [ln["sku"] for ln in page1] == ["S0", "S1", "S2", "S3"]
    assert [ln["sku"] for ln in page2] == ["S4", "S5", "S6", "S7"]


def test_add_manifest_line_assigns_next_line_no_and_bumps_count(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    _account_id, manifest_id = _seeded_manifest(conn, n=3)
    before_count = db.get_manifest(conn, manifest_id)["line_count"]

    line_id = db.add_manifest_line(conn, manifest_id, "NEW", "New item", 2, "9999999")
    line = db.get_manifest_line(conn, line_id)
    assert line["line_no"] == 4
    assert db.get_manifest(conn, manifest_id)["line_count"] == before_count + 1


def test_update_manifest_line_changes_fields(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    _account_id, manifest_id = _seeded_manifest(conn, n=3)
    lines = db.get_manifest_lines(conn, manifest_id)
    target = lines[0]

    db.update_manifest_line(conn, target["id"], "EDITED-SKU", "Edited desc", 5, "5555555")
    updated = db.get_manifest_line(conn, target["id"])
    assert updated["sku"] == "EDITED-SKU"
    assert updated["description"] == "Edited desc"
    assert updated["qty_expected"] == 5
    assert updated["raw_barcode"] == "5555555"
    assert updated["line_no"] == target["line_no"]  # unchanged


def test_delete_manifest_line_removes_row_and_keys_and_decrements_count(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    _account_id, manifest_id = _seeded_manifest(conn, n=3)
    lines = db.get_manifest_lines(conn, manifest_id)
    target_id = lines[0]["id"]
    before_count = db.get_manifest(conn, manifest_id)["line_count"]

    db.delete_manifest_line(conn, target_id)

    assert db.get_manifest_line(conn, target_id) is None
    assert db.get_manifest(conn, manifest_id)["line_count"] == before_count - 1
    remaining_keys = db.query(
        conn, "SELECT * FROM line_keys WHERE manifest_line_id = ?", (target_id,)
    )
    assert remaining_keys == []


def test_regenerate_keys_after_edit_reflects_new_barcode(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_id, manifest_id = _seeded_manifest(conn, n=3)
    lines = db.get_manifest_lines(conn, manifest_id)
    target_id = lines[0]["id"]

    db.update_manifest_line(conn, target_id, "S0", "D0", 1, "025300000208")
    account = db.get_account(conn, account_id)
    manifest_ingest.regenerate_keys(
        conn, manifest_id, bool(account["loose_match_enabled"]), account["loose_suffix_len"]
    )

    idx = manifest_ingest.build_match_index(conn, account, [manifest_id])
    result = idx.match("02532038")  # UPC-E form of the new barcode
    assert result.is_resolved
    assert result.manifest_line_id == target_id


def test_add_line_then_regenerate_makes_it_matchable(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_id, manifest_id = _seeded_manifest(conn, n=3)
    line_id = db.add_manifest_line(
        conn, manifest_id, "BRAND-NEW", "Brand new item", 1, "0000055555"
    )

    account = db.get_account(conn, account_id)
    manifest_ingest.regenerate_keys(
        conn, manifest_id, bool(account["loose_match_enabled"]), account["loose_suffix_len"]
    )

    idx = manifest_ingest.build_match_index(conn, account, [manifest_id])
    result = idx.match("0000055555")
    assert result.is_resolved
    assert result.manifest_line_id == line_id


def test_regenerate_keys_bumps_bundle_version_for_shifts_using_manifest(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_id, manifest_id = _seeded_manifest(conn, n=3)
    shift_id = db.create_shift(
        conn, account_id, "S", "2026-07-25", "tok", "2099-01-01T00:00:00Z", "h", 0
    )
    db.link_shift_manifest(conn, shift_id, manifest_id)
    before_version = db.get_shift(conn, shift_id)["bundle_version"]

    account = db.get_account(conn, account_id)
    manifest_ingest.regenerate_keys(
        conn, manifest_id, bool(account["loose_match_enabled"]), account["loose_suffix_len"]
    )

    assert db.get_shift(conn, shift_id)["bundle_version"] == before_version + 1
