import auth
import db


def test_generate_relay_password_has_no_ambiguous_characters():
    for _ in range(50):
        pw = auth.generate_relay_password()
        assert len(pw) == 14
        for bad in "0O1lI":
            assert bad not in pw


def test_generate_relay_password_is_random():
    passwords = {auth.generate_relay_password() for _ in range(20)}
    assert len(passwords) == 20  # no collisions in 20 draws


def test_update_user_password_changes_hash(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_id = db.create_account(conn, "Acme", 900, 25)
    user_id = db.create_user(
        conn, account_id, "owner@acme.test", auth.hash_password("old-password")
    )

    new_password = auth.generate_relay_password()
    db.update_user_password(conn, user_id, auth.hash_password(new_password))

    user = db.get_user(conn, user_id)
    assert auth.verify_password(new_password, user["pw_hash"])
    assert not auth.verify_password("old-password", user["pw_hash"])


def test_list_users_for_account_scoped_correctly(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    account_a = db.create_account(conn, "A", 900, 25)
    account_b = db.create_account(conn, "B", 900, 25)
    db.create_user(conn, account_a, "a1@test.com", auth.hash_password("x"))
    db.create_user(conn, account_a, "a2@test.com", auth.hash_password("x"), role="manager")
    db.create_user(conn, account_b, "b1@test.com", auth.hash_password("x"))

    users_a = db.list_users_for_account(conn, account_a)
    assert {u["email"] for u in users_a} == {"a1@test.com", "a2@test.com"}
    users_b = db.list_users_for_account(conn, account_b)
    assert {u["email"] for u in users_b} == {"b1@test.com"}
