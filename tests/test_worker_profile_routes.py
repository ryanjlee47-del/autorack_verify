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
    return conn, info["account_id"], client


def test_rename_worker_route(tmp_path):
    conn, account_id, client = _client(tmp_path)
    worker_id = db.get_or_create_worker(conn, account_id, "Bob")
    resp = client.post(
        f"/workers/{worker_id}/rename", data={"display_name": "Robert"}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert db.get_worker(conn, worker_id)["display_name"] == "Robert"
    audit = db.list_audit_log(conn)
    assert any(a["action"] == "worker.rename" for a in audit)


def test_rename_worker_rejects_blank_name(tmp_path):
    conn, account_id, client = _client(tmp_path)
    worker_id = db.get_or_create_worker(conn, account_id, "Bob")
    resp = client.post(
        f"/workers/{worker_id}/rename", data={"display_name": "  "}, follow_redirects=False
    )
    assert resp.status_code == 302  # redirected back with a flash, not saved
    assert db.get_worker(conn, worker_id)["display_name"] == "Bob"


def test_toggle_active_route(tmp_path):
    conn, account_id, client = _client(tmp_path)
    worker_id = db.get_or_create_worker(conn, account_id, "Bob")
    client.post(f"/workers/{worker_id}/toggle-active")
    assert db.get_worker(conn, worker_id)["active"] == 0
    client.post(f"/workers/{worker_id}/toggle-active")
    assert db.get_worker(conn, worker_id)["active"] == 1


def test_merge_route(tmp_path):
    conn, account_id, client = _client(tmp_path)
    worker_a = db.get_or_create_worker(conn, account_id, "Bob")
    worker_b = db.get_or_create_worker(conn, account_id, "bob")
    resp = client.post(
        f"/workers/{worker_b}/merge", data={"into_worker_id": str(worker_a)}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert db.get_worker(conn, worker_b) is None


def test_merge_route_rejects_missing_target(tmp_path):
    conn, account_id, client = _client(tmp_path)
    worker_id = db.get_or_create_worker(conn, account_id, "Bob")
    resp = client.post(f"/workers/{worker_id}/merge", data={}, follow_redirects=False)
    assert resp.status_code == 302
    assert db.get_worker(conn, worker_id) is not None  # nothing happened


def test_cannot_rename_another_accounts_worker(tmp_path):
    conn, account_id, client = _client(tmp_path)
    other_account_id = db.create_account(conn, "Other Co", 900, 25)
    other_worker_id = db.get_or_create_worker(conn, other_account_id, "Stranger")

    resp = client.post(
        f"/workers/{other_worker_id}/rename",
        data={"display_name": "Hacked"},
        follow_redirects=False,
    )
    assert resp.status_code == 404
    assert db.get_worker(conn, other_worker_id)["display_name"] == "Stranger"


def test_cannot_merge_into_another_accounts_worker(tmp_path):
    conn, account_id, client = _client(tmp_path)
    worker_id = db.get_or_create_worker(conn, account_id, "Bob")
    other_account_id = db.create_account(conn, "Other Co", 900, 25)
    other_worker_id = db.get_or_create_worker(conn, other_account_id, "Stranger")

    resp = client.post(
        f"/workers/{worker_id}/merge",
        data={"into_worker_id": str(other_worker_id)},
        follow_redirects=False,
    )
    assert resp.status_code == 302  # rejected with a flash, not a crash
    assert db.get_worker(conn, worker_id) is not None
    assert db.get_worker(conn, other_worker_id) is not None
