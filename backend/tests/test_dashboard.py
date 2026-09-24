"""Dashboard numbers, exports, and platform concerns (health, headers, config)."""

from __future__ import annotations

import csv
import io
import uuid

from conftest import make_order, scan, signup, worker_on_phone

from autorack.config import Settings


def test_summary_counts_today(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    done = make_order(client, owner, [("012345678905", 1)])["id"]
    open_ = make_order(client, owner, [("036000291452", 2)])["id"]
    make_order(client, owner)  # pending
    scan(client, phone, done, "wrong")
    scan(client, phone, done, "012345678905")
    scan(client, phone, done, "012345678905")  # over-pick
    scan(client, phone, open_, "036000291452")
    s = client.get("/api/dashboard/summary", headers=owner.h).json()
    assert s["orders"] == {**s["orders"], "pending": 1, "in_progress": 1, "completed_today": 1}
    t = s["today"]
    assert (t["scans"], t["units_picked"], t["mismatches"], t["over_picks"], t["errors_caught"]) == (4, 2, 1, 1, 2)
    assert t["accuracy"] == 0.5 and t["active_workers"] == 1


def test_live_view_and_problems(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner, "Devon")
    order = make_order(client, owner, [("012345678905", 2)], number="LIVE-1")
    scan(client, phone, order["id"], "WRONG-THING", intended_line_item_id=order["lines"][0]["id"])
    live = client.get("/api/dashboard/live", headers=owner.h).json()
    row = live["orders"][0]
    assert row["external_order_number"] == "LIVE-1" and row["errors_caught"] == 1 and row["status"] == "in_progress"
    p = live["problems"][0]
    assert p["worker"] == "Devon" and p["scanned_barcode"] == "WRONG-THING" and p["intended_sku"] == "SKU-0"


def test_worker_outlier_detection(client):
    owner = signup(client)
    careful = worker_on_phone(client, owner, "Careful")
    sloppy = worker_on_phone(client, owner, "Sloppy")
    oid = make_order(client, owner, [("012345678905", 500)])["id"]
    for i in range(40):
        scan(client, careful, oid, "012345678905")
        scan(client, sloppy, oid, "012345678905" if i % 3 else "WRONG")
    stats = client.get("/api/dashboard/workers", headers=owner.h).json()
    by_name = {w["name"]: w for w in stats["workers"]}
    assert by_name["Sloppy"]["needs_attention"] and not by_name["Careful"]["needs_attention"]
    assert by_name["Sloppy"]["mismatches"] == 14


def test_sku_insights_use_intended_line(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    order = make_order(client, owner, [("012345678905", 3), ("036000291452", 1)])
    widget = order["lines"][0]["id"]
    for _ in range(3):
        scan(client, phone, order["id"], "LOOKALIKE-1", intended_line_item_id=widget)
    ins = client.get("/api/dashboard/skus", headers=owner.h).json()
    assert ins["most_mispicked"][0] == {"barcode": "012345678905", "sku": "SKU-0", "description": None, "errors": 3}
    assert ins["most_grabbed_wrong"][0]["barcode"] == "LOOKALIKE-1"


def test_trend_has_one_point_per_day(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner)["id"]
    scan(client, phone, oid, "012345678905")
    t = client.get("/api/dashboard/trend", params={"days": 14}, headers=owner.h).json()
    assert len(t["series"]) == 14 and t["series"][-1]["units_picked"] == 1


def test_scan_export_neutralizes_formulas(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner)["id"]
    scan(client, phone, oid, '=HYPERLINK("http://evil","x")')
    r = client.get("/api/exports/scans.csv", headers=owner.h)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows[0]["scanned_barcode"].startswith("'=")
    assert rows[0]["result"] == "mismatch"


def test_orders_export(client):
    owner = signup(client)
    make_order(client, owner, number="EXP-1")
    rows = list(csv.DictReader(io.StringIO(client.get("/api/exports/orders.csv", headers=owner.h).text)))
    assert rows[0]["order_number"] == "EXP-1" and rows[0]["status"] == "pending"


def test_audit_log_records_owner_actions(client):
    owner = signup(client)
    make_order(client, owner, number="AUD-1")
    client.post("/api/workers", json={"name": "New"}, headers=owner.h)
    actions = [e["action"] for e in client.get("/api/audit", headers=owner.h).json()]
    assert {"warehouse.created", "user.login", "order.created", "worker.created"} <= set(actions)


def test_settings_change_bumps_open_orders(client):
    owner = signup(client)
    order = make_order(client, owner)
    r = client.patch("/api/warehouse", json={"loose_match_enabled": True, "suffix_len": 7}, headers=owner.h)
    assert r.status_code == 200 and r.json()["suffix_len"] == 7
    assert client.get(f"/api/orders/{order['id']}", headers=owner.h).json()["version"] > order["version"]
    assert client.patch("/api/warehouse", json={"suffix_len": 3}, headers=owner.h).status_code == 422
    assert client.patch("/api/warehouse", json={"timezone": "Mars/Base"}, headers=owner.h).status_code == 400


# ---------------------------------------------------------------------------
# Platform
# ---------------------------------------------------------------------------


def test_health(client):
    assert client.get("/api/health").json()["ok"] is True


def test_security_headers_and_cors(client):
    r = client.get("/api/public/config", headers={"Origin": "https://app.autorack.test"})
    assert r.headers["access-control-allow-origin"] == "https://app.autorack.test"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert "x-request-id" in r.headers
    r = client.get("/api/public/config", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_errors_have_stable_shape(client):
    owner = signup(client)
    r = client.get(f"/api/orders/{uuid.uuid4()}", headers=owner.h)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found"
    r = client.post("/api/orders", json={"lines": []}, headers=owner.h)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "validation_error"


def test_frontend_served_with_csp(client):
    r = client.get("/w/")
    if r.status_code == 404:
        return  # frontend not present in this checkout
    assert "content-security-policy" in r.headers
    assert r.headers["cache-control"] == "no-cache"


def test_production_config_validation():
    bad = Settings(environment="production", secret_key="short", email_backend="console", frontend_url="http://x")
    problems = bad.validate_for_production()
    assert any("SECRET_KEY" in p for p in problems)
    assert any("EMAIL_BACKEND" in p for p in problems)
    assert any("https" in p for p in problems)
    good = Settings(
        environment="production", secret_key="x" * 40, email_backend="smtp", frontend_url="https://app.example.com"
    )
    assert good.validate_for_production() == []


def test_neon_url_is_rewritten_for_psycopg3():
    s = Settings(database_url="postgresql://u:p@ep-cool.neon.tech/db?sslmode=require")
    assert s.database_url.startswith("postgresql+psycopg://")
