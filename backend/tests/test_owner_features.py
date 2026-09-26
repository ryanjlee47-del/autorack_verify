"""Roles (supervisor), several warehouses per sign-in, reports, the floor
board, money saved, and onboarding."""

from __future__ import annotations

from conftest import (
    Owner,
    last_link_token,
    make_order,
    scan,
    scan_event,
    signup,
    sync,
    worker_on_phone,
)

UPC_A = "012345678905"
UPC_B = "036000291452"


def invite_and_sign_in(client, owner: Owner, addr: str, role: str) -> Owner:
    r = client.post("/api/team", json={"email": addr, "role": role}, headers=owner.h)
    assert r.status_code == 201, r.text
    token = client.post("/api/auth/verify", json={"token": last_link_token(addr)}).json()["token"]
    return Owner(token=token, email=addr, warehouse_id=owner.warehouse_id)


def test_supervisor_resolves_problems_but_cannot_change_orders_or_see_billing(client):
    owner = signup(client)
    sup = invite_and_sign_in(client, owner, "lead@example.com", "supervisor")
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner)["id"]
    flag = {**scan_event(phone, oid, ""), "kind": "flag", "reason": "damaged", "scanned_barcode": None}
    sync(client, phone, flag)

    me = client.get("/api/auth/me", headers=sup.h).json()
    assert me["user"]["role"] == "supervisor"
    for url in ("/api/dashboard/summary", "/api/dashboard/live", "/api/orders", "/api/reports", "/api/workers"):
        assert client.get(url, headers=sup.h).status_code == 200, url
    r = client.post(f"/api/orders/{oid}/flags/{flag['id']}/resolve", json={"note": "ok"}, headers=sup.h)
    assert r.status_code == 200 and r.json()["flags"][0]["resolved_at"]

    denied = [
        ("POST", "/api/orders", {"lines": [{"barcode": "1"}]}),
        ("PATCH", f"/api/orders/{oid}", {"notes": "x"}),
        ("POST", f"/api/orders/{oid}/cancel", None),
        ("POST", "/api/workers", {"name": "x"}),
        ("POST", "/api/aliases", {"scanned_barcode": "1", "target_barcode": "2"}),
        ("POST", "/api/onboarding/sample", None),
        ("GET", "/api/billing", None),
        ("PATCH", "/api/warehouse", {"name": "x"}),
        ("POST", "/api/team", {"email": "x@example.com"}),
    ]
    for method, url, body in denied:
        r = client.request(method, url, json=body, headers=sup.h)
        assert r.status_code == 403, (method, url, r.status_code)


def test_manager_runs_orders_but_not_billing(client):
    owner = signup(client)
    mgr = invite_and_sign_in(client, owner, "mgr@example.com", "manager")
    assert client.post("/api/orders", json={"lines": [{"barcode": "1"}]}, headers=mgr.h).status_code == 201
    assert client.get("/api/billing", headers=mgr.h).status_code == 403
    assert client.patch("/api/warehouse", json={"name": "x"}, headers=mgr.h).status_code == 403


def test_one_person_several_warehouses(client):
    owner = signup(client, "North Dock")
    r = client.post("/api/auth/warehouses", json={"name": "South Dock"}, headers=owner.h)
    assert r.status_code == 201
    south_id = r.json()["id"]
    me = client.get("/api/auth/me", headers=owner.h).json()
    assert me["warehouse"]["name"] == "South Dock"  # switched to the new one
    assert sorted(w["name"] for w in me["warehouses"]) == ["North Dock", "South Dock"]
    make_order(client, owner, number="SOUTH-1")

    client.post("/api/auth/switch", json={"warehouse_id": owner.warehouse_id}, headers=owner.h)
    orders = client.get("/api/orders", headers=owner.h).json()["orders"]
    assert [o["external_order_number"] for o in orders] == []
    client.post("/api/auth/switch", json={"warehouse_id": south_id}, headers=owner.h)
    orders = client.get("/api/orders", headers=owner.h).json()["orders"]
    assert [o["external_order_number"] for o in orders] == ["SOUTH-1"]

    # Someone else's warehouse is off limits.
    stranger = signup(client, "Elsewhere")
    r = client.post("/api/auth/switch", json={"warehouse_id": stranger.warehouse_id}, headers=owner.h)
    assert r.status_code == 403


def test_inviting_an_existing_user_adds_a_membership(client):
    a = signup(client, "A")
    b = signup(client, "B")
    r = client.post("/api/team", json={"email": b.email, "role": "manager"}, headers=a.h)
    assert r.status_code == 201
    me = client.get("/api/auth/me", headers=b.h).json()
    assert {w["name"]: w["role"] for w in me["warehouses"]} == {"A": "manager", "B": "owner"}
    # Removing them from A doesn't sign them out of B.
    r = client.patch(f"/api/team/{r.json()['id']}", json={"active": False}, headers=a.h)
    assert r.status_code == 200
    me = client.get("/api/auth/me", headers=b.h).json()
    assert [w["name"] for w in me["warehouses"]] == ["B"]
    assert client.post("/api/team", json={"email": b.email}, headers=a.h).status_code == 201  # re-invite works


def test_last_owner_is_protected(client):
    owner = signup(client)
    me = client.get("/api/auth/me", headers=owner.h).json()
    r = client.patch(f"/api/team/{me['user']['id']}", json={"role": "manager"}, headers=owner.h)
    assert r.json()["detail"]["code"] == "last_owner"


def test_preferences(client):
    owner = signup(client)
    r = client.patch("/api/auth/preferences", json={"email_daily_summary": False, "name": "Pat"}, headers=owner.h)
    assert r.json() == {"email_daily_summary": False, "email_alerts": True, "name": "Pat"}
    assert client.get("/api/auth/me", headers=owner.h).json()["membership"]["email_daily_summary"] is False


def test_money_saved_uses_the_warehouse_cost(client):
    owner = signup(client)
    client.patch("/api/warehouse", json={"cost_per_error_cents": 4200}, headers=owner.h)
    phone = worker_on_phone(client, owner)
    oid = make_order(client, owner, [(UPC_A, 1)])["id"]
    scan(client, phone, oid, "999999999993")
    scan(client, phone, oid, "888888888884")
    s = client.get("/api/dashboard/summary", headers=owner.h).json()
    assert s["today"]["errors_caught"] == 2
    assert s["today"]["money_saved_cents"] == 8400
    trend = client.get("/api/dashboard/trend", headers=owner.h).json()["series"]
    assert trend[-1]["money_saved_cents"] == 8400


def test_report_by_customer_sku_and_worker(client):
    owner = signup(client)
    phone = worker_on_phone(client, owner)
    a = client.post(
        "/api/orders",
        json={"customer": "Acme", "lines": [{"barcode": UPC_A, "quantity": 2, "sku": "WID"}]},
        headers=owner.h,
    ).json()
    b = client.post(
        "/api/orders", json={"lines": [{"barcode": UPC_B, "quantity": 1, "sku": "TAPE"}]}, headers=owner.h
    ).json()
    scan(client, phone, a["id"], UPC_A)
    scan(client, phone, a["id"], UPC_B, intended_line_item_id=a["lines"][0]["id"])  # wrong item
    scan(client, phone, b["id"], UPC_B)
    r = client.get("/api/reports", headers=owner.h)
    assert r.status_code == 200
    rep = r.json()
    assert rep["totals"]["units_picked"] == 2
    assert rep["totals"]["errors_caught"] == 1
    assert rep["totals"]["money_saved_cents"] == 5000
    customers = {c["customer"]: c for c in rep["customers"]}
    assert customers["Acme"]["errors_caught"] == 1 and customers["Acme"]["units_picked"] == 1
    assert customers["(no customer)"]["units_picked"] == 1
    skus = {s["sku"]: s for s in rep["skus"]}
    assert skus["WID"]["mispicks"] == 1
    assert rep["workers"][0]["name"] == "Maria" and rep["workers"][0]["errors"] == 1
    assert len(rep["days"]) == 30
    r = client.get("/api/reports", params={"from": "2026-13-01"}, headers=owner.h)
    assert r.status_code == 400
    r = client.get("/api/reports", params={"from": "2020-01-01", "to": "2026-01-01"}, headers=owner.h)
    assert r.json()["detail"]["code"] == "range_too_long"


def test_csv_customer_column(client):
    owner = signup(client)
    csv = b"Order #,Ship To Name,UPC,Qty\nSO-1,Acme Co,012345678905,2\nSO-1,Acme Co,036000291452,1\n"
    r = client.post("/api/orders/import", files={"file": ("x.csv", csv, "text/csv")}, headers=owner.h)
    assert r.status_code == 201, r.text
    orders = client.get("/api/orders", params={"q": "acme"}, headers=owner.h).json()["orders"]
    assert [o["customer"] for o in orders] == ["Acme Co"]


def test_floor_board_is_opt_in(client):
    owner = signup(client)
    assert client.get("/api/dashboard/board", headers=owner.h).status_code == 403
    client.patch("/api/warehouse", json={"leaderboard_enabled": True}, headers=owner.h)
    maria = worker_on_phone(client, owner, "Maria")
    devon = worker_on_phone(client, owner, "Devon")
    oid = make_order(client, owner, [(UPC_A, 3)])["id"]
    scan(client, maria, oid, UPC_A)
    scan(client, maria, oid, UPC_A)
    scan(client, devon, oid, UPC_A)
    board = client.get("/api/dashboard/board", headers=owner.h).json()
    assert [(w["rank"], w["name"], w["units_picked"]) for w in board["workers"]] == [(1, "Maria", 2), (2, "Devon", 1)]
    assert board["workers"][0]["orders_completed"] == 1
    assert client.get("/api/auth/me", headers=owner.h).json()["warehouse"]["leaderboard_enabled"] is True


def test_onboarding_checklist_and_sample_orders(client):
    owner = signup(client)
    ob = client.get("/api/onboarding", headers=owner.h).json()
    assert ob["done"] == 0 and not ob["sample_loaded"]
    r = client.post("/api/onboarding/sample", headers=owner.h)
    assert r.status_code == 201
    assert r.json()["orders_created"] == 12
    assert client.post("/api/onboarding/sample", headers=owner.h).status_code == 409
    orders = client.get("/api/orders", params={"limit": 50}, headers=owner.h).json()["orders"]
    assert len(orders) == 12
    assert all(o["source"] == "sample" and o["customer"] for o in orders)
    phone = worker_on_phone(client, owner)
    ob = client.get("/api/onboarding", headers=owner.h).json()
    done = {s["key"] for s in ob["steps"] if s["done"]}
    assert done == {"worker", "phone", "orders"}
    order = client.get(f"/api/orders/{orders[0]['id']}", headers=owner.h).json()
    scan(client, phone, order["id"], order["lines"][0]["expected_barcode"])
    assert "scan" in {s["key"] for s in client.get("/api/onboarding", headers=owner.h).json()["steps"] if s["done"]}
    assert client.post("/api/onboarding/dismiss", headers=owner.h).json()["dismissed"] is True
