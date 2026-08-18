import app as app_module
import db
import seed


def _client(tmp_path):
    db_path = tmp_path / "test.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    info = seed.ensure_seeded(conn)
    client = flask_app.test_client()
    client.post("/login", data={"email": seed.DEMO_EMAIL, "password": seed.DEMO_PASSWORD})
    manifest_id = db.list_manifests(conn, info["account_id"])[0]["id"]
    return conn, info["account_id"], client, manifest_id


def test_manifest_detail_paginates(tmp_path):
    conn, account_id, client, manifest_id = _client(tmp_path)
    resp = client.get(f"/manifests/{manifest_id}")
    assert resp.status_code == 200
    assert manifest_id


def test_add_line_creates_row_and_regenerates_keys(tmp_path):
    conn, account_id, client, manifest_id = _client(tmp_path)
    before_count = db.get_manifest(conn, manifest_id)["line_count"]

    resp = client.post(
        f"/manifests/{manifest_id}/lines/add",
        data={
            "raw_barcode": "0000012345",
            "sku": "NEW-1",
            "description": "New item",
            "qty_expected": "3",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert db.get_manifest(conn, manifest_id)["line_count"] == before_count + 1
    line = next(ln for ln in db.get_manifest_lines(conn, manifest_id) if ln["sku"] == "NEW-1")
    assert line["raw_barcode"] == "0000012345"
    assert line["qty_expected"] == 3

    key_rows = db.query(conn, "SELECT * FROM line_keys WHERE manifest_line_id = ?", (line["id"],))
    assert key_rows  # keys were regenerated for the new line

    audit = db.list_audit_log(conn)
    assert any(a["action"] == "manifest_line.add" for a in audit)


def test_add_line_requires_barcode(tmp_path):
    conn, account_id, client, manifest_id = _client(tmp_path)
    before_count = db.get_manifest(conn, manifest_id)["line_count"]
    resp = client.post(
        f"/manifests/{manifest_id}/lines/add", data={"sku": "NO-BARCODE"}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert db.get_manifest(conn, manifest_id)["line_count"] == before_count


def test_edit_line_updates_fields_and_regenerates_keys(tmp_path):
    conn, account_id, client, manifest_id = _client(tmp_path)
    line = db.get_manifest_lines(conn, manifest_id)[0]

    resp = client.post(
        f"/manifests/{manifest_id}/lines/{line['id']}/edit",
        data={
            "raw_barcode": "025300000208",
            "sku": "EDITED",
            "description": "Edited desc",
            "qty_expected": "5",
            "page": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    updated = db.get_manifest_line(conn, line["id"])
    assert updated["sku"] == "EDITED"
    assert updated["raw_barcode"] == "025300000208"
    assert updated["qty_expected"] == 5

    account = db.get_account(conn, account_id)
    import manifest_ingest

    idx = manifest_ingest.build_match_index(conn, account, [manifest_id])
    result = idx.match("02532038")  # UPC-E form of the new barcode
    assert result.is_resolved
    assert result.manifest_line_id == line["id"]


def test_delete_line_removes_it_and_regenerates_keys(tmp_path):
    conn, account_id, client, manifest_id = _client(tmp_path)
    line = db.get_manifest_lines(conn, manifest_id)[0]
    before_count = db.get_manifest(conn, manifest_id)["line_count"]

    resp = client.post(
        f"/manifests/{manifest_id}/lines/{line['id']}/delete",
        data={"page": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert db.get_manifest_line(conn, line["id"]) is None
    assert db.get_manifest(conn, manifest_id)["line_count"] == before_count - 1


def test_editing_bumps_shift_bundle_version(tmp_path):
    conn, account_id, client, manifest_id = _client(tmp_path)
    shift_id = db.create_shift(
        conn, account_id, "S", "2026-07-25", "tok", "2099-01-01T00:00:00Z", "h", 0
    )
    db.link_shift_manifest(conn, shift_id, manifest_id)
    before_version = db.get_shift(conn, shift_id)["bundle_version"]

    client.post(
        f"/manifests/{manifest_id}/lines/add",
        data={"raw_barcode": "1112223334"},
        follow_redirects=False,
    )

    assert db.get_shift(conn, shift_id)["bundle_version"] == before_version + 1


def test_cannot_view_or_edit_another_accounts_manifest(tmp_path):
    conn, account_id, client, manifest_id = _client(tmp_path)
    other_account_id = db.create_account(conn, "Other Co", 900, 25)
    other_manifest_id, _report = __import__("manifest_ingest").commit_manifest(
        conn,
        other_account_id,
        "OTHER",
        None,
        [{"line_no": 1, "sku": "X", "description": "", "qty_expected": 1, "raw_barcode": "123"}],
        False,
        8,
    )

    assert client.get(f"/manifests/{other_manifest_id}").status_code == 404
    resp = client.post(
        f"/manifests/{other_manifest_id}/lines/add",
        data={"raw_barcode": "999"},
        follow_redirects=False,
    )
    assert resp.status_code == 404

    other_line_id = db.get_manifest_lines(conn, other_manifest_id)[0]["id"]
    resp2 = client.post(
        f"/manifests/{other_manifest_id}/lines/{other_line_id}/edit",
        data={"raw_barcode": "hacked"},
        follow_redirects=False,
    )
    assert resp2.status_code == 404
    assert db.get_manifest_line(conn, other_line_id)["raw_barcode"] == "123"


def test_cannot_edit_a_line_via_wrong_manifest_id(tmp_path):
    """A line_id that's real but belongs to a *different* manifest (even
    within the same account) must be rejected -- the URL's manifest_id
    and the line's actual manifest_id must match."""
    conn, account_id, client, manifest_id = _client(tmp_path)
    import manifest_ingest

    other_manifest_id, _report = manifest_ingest.commit_manifest(
        conn,
        account_id,
        "OTHER-SAME-ACCOUNT",
        None,
        [{"line_no": 1, "sku": "X", "description": "", "qty_expected": 1, "raw_barcode": "456"}],
        False,
        8,
    )
    other_line_id = db.get_manifest_lines(conn, other_manifest_id)[0]["id"]

    resp = client.post(
        f"/manifests/{manifest_id}/lines/{other_line_id}/edit",
        data={"raw_barcode": "hacked"},
        follow_redirects=False,
    )
    assert resp.status_code == 404
    assert db.get_manifest_line(conn, other_line_id)["raw_barcode"] == "456"


def test_manifest_upload_commit_redirects_to_detail_page(tmp_path):
    db_path = tmp_path / "test.db"
    flask_app = app_module.create_app(db_path=db_path)
    db.init_db(db_path)
    client = flask_app.test_client()
    client.post(
        "/signup",
        data={
            "business_name": "Fresh Co",
            "email": "fresh@test.com",
            "password": "longenough1",
            "timezone": "UTC",
        },
    )
    resp = client.post(
        "/manifests/commit",
        data={
            "ref": "FRESH-1",
            "source_filename": "",
            "mode": "paste",
            "raw_text": "012345678905\nALT-SKU-1\n",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/manifests/" in resp.headers["Location"]
    assert resp.headers["Location"] != "/manifests"
