import base64

import admin_api
import app as app_module
import db
import seed


def _client_with_token(tmp_path):
    db_path = tmp_path / "test.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    info = seed.ensure_seeded(conn)
    token = admin_api._load_or_create_token(db_path.parent / admin_api.TOKEN_FILENAME)
    client = flask_app.test_client()
    return client, conn, info, token


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_ping_requires_valid_token(tmp_path):
    client, conn, info, token = _client_with_token(tmp_path)
    assert client.get("/admin-api/ping").status_code == 401
    assert client.get("/admin-api/ping", headers=_auth("wrong-token")).status_code == 401
    resp = client.get("/admin-api/ping", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_session_cookie_is_rejected_even_with_valid_token(tmp_path):
    client, conn, info, token = _client_with_token(tmp_path)
    login = client.post("/login", data={"email": seed.DEMO_EMAIL, "password": seed.DEMO_PASSWORD})
    assert login.status_code == 302
    resp = client.get("/admin-api/ping", headers=_auth(token))
    assert resp.status_code == 401


def test_rpc_unknown_operation_404s(tmp_path):
    client, conn, info, token = _client_with_token(tmp_path)
    resp = client.post("/admin-api/rpc", json={"fn": "not_a_real_op"}, headers=_auth(token))
    assert resp.status_code == 404


def test_rpc_list_accounts_read_only(tmp_path):
    client, conn, info, token = _client_with_token(tmp_path)
    resp = client.post("/admin-api/rpc", json={"fn": "list_accounts"}, headers=_auth(token))
    assert resp.status_code == 200
    accounts = resp.get_json()["result"]
    assert any(a["id"] == info["account_id"] for a in accounts)


def test_rpc_overview_stats(tmp_path):
    client, conn, info, token = _client_with_token(tmp_path)
    resp = client.post("/admin-api/rpc", json={"fn": "overview_stats"}, headers=_auth(token))
    assert resp.status_code == 200
    result = resp.get_json()["result"]
    assert result["accounts"] >= 1
    assert len(result["hourly"]) == 24


def test_action_grant_credit_mutates_and_audits(tmp_path):
    client, conn, info, token = _client_with_token(tmp_path)
    account_id = info["account_id"]
    resp = client.post(
        "/admin-api/action",
        json={
            "fn": "account.grant_credit",
            "params": {"account_id": account_id, "cents": 500, "reason": "test grant"},
        },
        headers=_auth(token),
    )
    assert resp.status_code == 200

    audit_rows = db.list_audit_log(conn)
    matching = [
        a
        for a in audit_rows
        if a["action"] == "account.grant_credit" and a["target_id"] == str(account_id)
    ]
    assert matching
    assert matching[0]["actor"].startswith("operator-api:")

    credits = db.query(conn, "SELECT * FROM account_credits WHERE account_id = ?", (account_id,))
    assert any(c["cents"] == 500 and c["reason"] == "test grant" for c in credits)


def test_action_validation_error_returns_400_not_500(tmp_path):
    client, conn, info, token = _client_with_token(tmp_path)
    resp = client.post(
        "/admin-api/action",
        json={
            "fn": "account.grant_credit",
            "params": {"account_id": info["account_id"], "cents": 500, "reason": ""},
        },
        headers=_auth(token),
    )
    assert resp.status_code == 400


def test_action_cannot_demote_last_owner(tmp_path):
    client, conn, info, token = _client_with_token(tmp_path)
    resp = client.post(
        "/admin-api/action",
        json={"fn": "user.update", "params": {"user_id": info["user_id"], "role": "manager"}},
        headers=_auth(token),
    )
    assert resp.status_code == 400
    user = db.get_user(conn, info["user_id"])
    assert user["role"] == "owner"


def test_sql_console_read_only_blocks_writes_even_if_client_claims_unsafe_false(tmp_path):
    client, conn, info, token = _client_with_token(tmp_path)
    resp = client.post(
        "/admin-api/sql",
        json={"sql": "DELETE FROM accounts", "unsafe": False},
        headers=_auth(token),
    )
    assert resp.status_code == 400
    accounts = db.list_accounts(conn)
    assert len(accounts) >= 1


def test_sql_console_select_works_read_only(tmp_path):
    client, conn, info, token = _client_with_token(tmp_path)
    resp = client.post(
        "/admin-api/sql",
        json={"sql": "SELECT COUNT(*) AS n FROM accounts", "unsafe": False},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    result = resp.get_json()["result"]
    assert result["columns"] == ["n"]
    assert result["rows"][0][0] >= 1


def test_sql_console_unsafe_write_is_audited(tmp_path):
    client, conn, info, token = _client_with_token(tmp_path)
    resp = client.post(
        "/admin-api/sql",
        json={
            "sql": "UPDATE accounts SET plan = 'pro' WHERE id = " + str(info["account_id"]),
            "unsafe": True,
        },
        headers=_auth(token),
    )
    assert resp.status_code == 200
    account = db.get_account(conn, info["account_id"])
    assert account["plan"] == "pro"
    audit_rows = db.list_audit_log(conn)
    assert any(a["action"] == "sql.unsafe_execute" for a in audit_rows)


def test_action_account_create(tmp_path):
    client, conn, info, token = _client_with_token(tmp_path)
    resp = client.post(
        "/admin-api/action",
        json={
            "fn": "account.create",
            "params": {"name": "New Co", "email": "new-owner@example.com"},
        },
        headers=_auth(token),
    )
    assert resp.status_code == 200
    result = resp.get_json()["result"]
    assert result["email"] == "new-owner@example.com"
    assert result["password"]
    new_user = db.get_user_by_email(conn, "new-owner@example.com")
    assert new_user["role"] == "owner"


def test_rpc_table_page_clamps_limit(tmp_path):
    client, conn, info, token = _client_with_token(tmp_path)
    resp = client.post(
        "/admin-api/rpc",
        json={"fn": "table_page", "params": {"table": "accounts", "limit": 999999}},
        headers=_auth(token),
    )
    assert resp.status_code == 200


def test_rpc_table_page_rejects_unknown_table(tmp_path):
    client, conn, info, token = _client_with_token(tmp_path)
    resp = client.post(
        "/admin-api/rpc",
        json={"fn": "table_page", "params": {"table": "sqlite_master"}},
        headers=_auth(token),
    )
    assert resp.status_code == 400


def test_action_user_update_rejects_duplicate_email(tmp_path):
    client, conn, info, token = _client_with_token(tmp_path)
    other_id = db.create_user(conn, info["account_id"], "other@example.com", "x", role="manager")
    resp = client.post(
        "/admin-api/action",
        json={"fn": "user.update", "params": {"user_id": other_id, "email": seed.DEMO_EMAIL}},
        headers=_auth(token),
    )
    assert resp.status_code == 400
    assert db.get_user(conn, other_id)["email"] == "other@example.com"


def test_backup_and_restore_round_trip(tmp_path):
    client, conn, info, token = _client_with_token(tmp_path)
    backup_dir = tmp_path / "backup_data"

    import backup as backup_module

    original_dir = backup_module.DEFAULT_BACKUP_DIR
    backup_module.DEFAULT_BACKUP_DIR = backup_dir
    try:
        resp = client.post("/admin-api/backup", headers=_auth(token))
        assert resp.status_code == 200
        result = resp.get_json()["result"]
        assert result["db"]["filename"].endswith(".db")
        db_bytes = base64.b64decode(result["db"]["content_b64"])
        assert db_bytes[:16] == b"SQLite format 3\x00"

        conn.execute("UPDATE accounts SET plan = 'mutated' WHERE id = ?", (info["account_id"],))
        conn.commit()
        assert db.get_account(conn, info["account_id"])["plan"] == "mutated"

        resp = client.post(
            "/admin-api/restore",
            json={
                "kind": "db",
                "filename": result["db"]["filename"],
                "content_b64": result["db"]["content_b64"],
            },
            headers=_auth(token),
        )
        assert resp.status_code == 200
        assert db.get_account(conn, info["account_id"])["plan"] != "mutated"
    finally:
        backup_module.DEFAULT_BACKUP_DIR = original_dir


def test_backup_requires_auth(tmp_path):
    client, conn, info, token = _client_with_token(tmp_path)
    resp = client.post("/admin-api/backup")
    assert resp.status_code == 401
