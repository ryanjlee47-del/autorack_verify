from datetime import UTC

import app as app_module
import db
import i18n


def test_normalize_lang_defaults_to_english():
    assert i18n.normalize_lang(None) == "en"
    assert i18n.normalize_lang("fr") == "en"
    assert i18n.normalize_lang("ES") == "es"


def test_detect_lang_from_accept_header():
    assert i18n.detect_lang_from_accept_header("es-ES,es;q=0.9,en;q=0.8") == "es"
    assert i18n.detect_lang_from_accept_header("en-US,en;q=0.9") == "en"
    assert i18n.detect_lang_from_accept_header(None) == "en"
    assert i18n.detect_lang_from_accept_header("fr-FR") == "en"


def test_every_english_string_has_a_spanish_counterpart():
    assert set(i18n.STRINGS["en"].keys()) == set(i18n.STRINGS["es"].keys())


def test_t_falls_back_to_english_for_unknown_key():
    assert i18n.t("es", "nonexistent_key") == "nonexistent_key"


def test_join_page_defaults_to_english_with_no_signal(tmp_path):
    flask_app = app_module.create_app(db_path=tmp_path / "t.db")
    db.init_db(tmp_path / "t.db")
    resp = flask_app.test_client().get("/w")
    body = resp.data.decode()
    assert "Scan the QR code" in body
    assert "Español" in body  # toggle offers the OTHER language


def test_join_page_honors_accept_language_header(tmp_path):
    flask_app = app_module.create_app(db_path=tmp_path / "t.db")
    db.init_db(tmp_path / "t.db")
    resp = flask_app.test_client().get("/w", headers={"Accept-Language": "es-ES,es;q=0.9"})
    body = resp.data.decode()
    assert "Escanea el código QR" in body
    assert "English" in body


def test_lang_cookie_persists_across_requests(tmp_path):
    flask_app = app_module.create_app(db_path=tmp_path / "t.db")
    db.init_db(tmp_path / "t.db")
    client = flask_app.test_client()
    client.get("/w/lang/es", follow_redirects=False)
    resp = client.get("/w")
    assert "Escanea el código QR" in resp.data.decode()


def test_query_param_overrides_cookie(tmp_path):
    flask_app = app_module.create_app(db_path=tmp_path / "t.db")
    db.init_db(tmp_path / "t.db")
    client = flask_app.test_client()
    client.get("/w/lang/es", follow_redirects=False)
    resp = client.get("/w?lang=en")
    assert "Scan the QR code" in resp.data.decode()


def test_scan_screen_receives_lang_attribute(tmp_path):
    from datetime import datetime, timedelta

    import seed

    db_path = tmp_path / "t.db"
    flask_app = app_module.create_app(db_path=db_path)
    conn = db.init_db(db_path)
    info = seed.ensure_seeded(conn)
    manifests = db.list_manifests(conn, info["account_id"])
    token = "tok"
    expires = (datetime.now(UTC) + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    shift_id = db.create_shift(conn, info["account_id"], "S", "2026-07-25", token, expires, "h", 0)
    for m in manifests:
        db.link_shift_manifest(conn, shift_id, m["id"])

    client = flask_app.test_client()
    client.get("/w/lang/es", follow_redirects=False)
    join = client.post(
        "/w/join", data={"t": token, "name": "X", "lang": "es"}, follow_redirects=False
    )
    sid = join.headers["Location"].split("sid=")[1]
    scan_resp = client.get(f"/w/scan?sid={sid}")
    assert 'data-lang="es"' in scan_resp.data.decode()
