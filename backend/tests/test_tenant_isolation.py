"""No endpoint may return, or act on, another warehouse's data.

The design choice (no row-level security; every query scoped by warehouse_id)
is only as good as this test. It walks *every* registered route rather than a
hand-picked list, so a new endpoint is covered the moment it exists, and it
fails if a route with an id parameter isn't listed here with a request body.
"""

from __future__ import annotations

import re
import uuid

import pytest
from conftest import JPEG, add_worker, make_order, scan_event, signup, sync, worker_on_phone

# Bodies that pass validation, so the request reaches the tenancy check.
BODIES: dict[tuple[str, str], dict] = {
    ("PATCH", "/api/orders/{order_id}"): {"notes": "x"},
    ("POST", "/api/orders/{order_id}/lines"): {"barcode": "123"},
    ("PATCH", "/api/orders/{order_id}/lines/{line_id}"): {"quantity": 2},
    ("POST", "/api/orders/{order_id}/flags/{flag_id}/resolve"): {},
    ("PATCH", "/api/workers/{worker_id}"): {"name": "x"},
    ("POST", "/api/workers/{worker_id}/reset-pin"): {},
    ("PATCH", "/api/devices/{device_id}"): {"label": "x"},
    ("PATCH", "/api/team/{user_id}"): {"name": "x"},
}
NO_BODY = {
    ("GET", "/api/orders/{order_id}"),
    ("POST", "/api/orders/{order_id}/cancel"),
    ("DELETE", "/api/orders/{order_id}/lines/{line_id}"),
    ("GET", "/api/orders/{order_id}/scans"),
    ("GET", "/api/orders/{order_id}/proof"),
    ("GET", "/api/photos/{photo_id}"),
    ("POST", "/api/devices/{device_id}/revoke"),
    ("DELETE", "/api/aliases/{alias_id}"),
    ("GET", "/api/worker/orders/{order_id}"),
}


@pytest.fixture
def two_tenants(client):
    a = signup(client, "Warehouse A")
    b = signup(client, "Warehouse B")
    a_phone = worker_on_phone(client, a, "A-Worker")
    b_phone = worker_on_phone(client, b, "B-SECRET-WORKER")
    b_order = make_order(client, b, [("B-SECRET-BARCODE", 2)], number="B-SECRET-ORDER")
    flag = {**scan_event(b_phone, b_order["id"], ""), "kind": "flag", "reason": "damaged", "scanned_barcode": None}
    sync(client, b_phone, scan_event(b_phone, b_order["id"], "B-SECRET-BARCODE"), flag)
    photo_id = str(uuid.uuid4())
    r = client.post(
        "/api/worker/photos",
        params={"id": photo_id, "flag_id": flag["id"]},
        content=JPEG,
        headers={**b_phone.h, "Content-Type": "image/jpeg"},
    )
    assert r.status_code == 201, r.text
    client.post("/api/aliases", json={"scanned_barcode": "B-ALIAS", "target_barcode": "B-SECRET-BARCODE"}, headers=b.h)
    client.post("/api/team", json={"email": "b-manager@example.com"}, headers=b.h)
    ids = {
        "order_id": b_order["id"],
        "line_id": b_order["lines"][0]["id"],
        "flag_id": client.get(f"/api/orders/{b_order['id']}", headers=b.h).json()["flags"][0]["id"],
        "worker_id": client.get("/api/workers", headers=b.h).json()["workers"][0]["worker_id"],
        "device_id": client.get("/api/devices", headers=b.h).json()[0]["id"],
        "user_id": next(u["id"] for u in client.get("/api/team", headers=b.h).json() if u["role"] == "manager"),
        "alias_id": client.get("/api/aliases", headers=b.h).json()[0]["id"],
        "photo_id": photo_id,
        "flag_event_id": flag["id"],
    }
    return a, a_phone, b, ids


def test_every_id_route_hides_other_tenants(app, client, two_tenants):
    a, a_phone, _b, ids = two_tenants
    checked = 0
    # The OpenAPI schema is the public, complete list of routes and methods.
    for path, operations in app.openapi()["paths"].items():
        if "{" not in path or path.startswith("/api/admin/"):
            continue  # admin routes cross tenants by design; see test_admin_routes_need_operator
        for method in operations:
            key = (method.upper(), path)
            assert key in BODIES or key in NO_BODY, f"New id route {key}: add it to this test"
            url = re.sub(r"\{(\w+)\}", lambda m: ids[m.group(1)], path)
            headers = a_phone.h if path.startswith("/api/worker/") else a.h
            r = client.request(method.upper(), url, headers=headers, json=BODIES.get(key))
            assert r.status_code == 404, f"{method} {path} -> {r.status_code} {r.text}"
            checked += 1
    assert checked == len(BODIES) + len(NO_BODY)


def test_lists_and_reports_never_include_other_tenants(client, two_tenants):
    a, a_phone, _b, ids = two_tenants
    owner_urls = [
        "/api/orders",
        "/api/orders?status=open",
        "/api/orders?q=SECRET",
        "/api/workers",
        "/api/devices",
        "/api/team",
        "/api/aliases",
        "/api/audit",
        "/api/imports",
        "/api/dashboard/summary",
        "/api/dashboard/live",
        "/api/dashboard/workers",
        "/api/dashboard/skus",
        "/api/dashboard/trend",
        "/api/dashboard/problems",
        "/api/exports/scans.csv",
        "/api/exports/orders.csv",
        f"/api/orders/pick-sheets?ids={ids['order_id']}",
    ]
    for url in owner_urls:
        r = client.get(url, headers=a.h)
        assert r.status_code == 200, (url, r.text)
        for secret in ("B-SECRET", "b-manager", "B-ALIAS", ids["order_id"], ids["worker_id"], ids["device_id"]):
            assert secret not in r.text, (url, secret)
    r = client.get("/api/worker/orders", headers=a_phone.h)
    assert "B-SECRET" not in r.text
    r = client.get("/api/worker/orders/lookup", params={"code": "B-SECRET-ORDER"}, headers=a_phone.h)
    assert r.status_code == 404
    r = client.get("/api/worker/orders/lookup", params={"code": f"AUTORACK:ORDER:{ids['order_id']}"}, headers=a_phone.h)
    assert r.status_code == 404


def test_sync_cannot_touch_other_tenants_orders(client, two_tenants):
    _a, a_phone, b, ids = two_tenants
    out = sync(client, a_phone, scan_event(a_phone, ids["order_id"], "B-SECRET-BARCODE"))
    assert out["events"][0]["error"]["code"] == "order_not_found"
    detail = client.get(f"/api/orders/{ids['order_id']}", headers=b.h).json()
    assert detail["lines"][0]["scanned_quantity"] == 1  # unchanged


def test_scan_ids_cannot_collide_across_tenants(client, two_tenants):
    a, a_phone, _b, _ids = two_tenants
    oid = make_order(client, a)["id"]
    ev = scan_event(a_phone, oid, "012345678905")
    sync(client, a_phone, ev)
    b2 = signup(client, "Warehouse C")
    c_phone = worker_on_phone(client, b2)
    c_order = make_order(client, b2)["id"]
    replay = {**scan_event(c_phone, c_order, "012345678905"), "id": ev["id"]}
    out = sync(client, c_phone, replay)
    assert out["events"][0]["error"]["code"] == "id_conflict"


def test_worker_assignment_rejects_foreign_worker(client, two_tenants):
    a, _a_phone, _b, ids = two_tenants
    oid = make_order(client, a)["id"]
    r = client.patch(f"/api/orders/{oid}", json={"assigned_worker_id": ids["worker_id"]}, headers=a.h)
    assert r.status_code == 400
    r = client.post(
        "/api/orders",
        json={"lines": [{"barcode": "1"}], "assigned_worker_id": ids["worker_id"]},
        headers=a.h,
    )
    assert r.status_code == 400
    assert add_worker(client, a, "fine")  # sanity: A can still manage its own
    assert str(uuid.UUID(ids["worker_id"]))


def test_admin_routes_need_operator(app, client, two_tenants):
    a, _a_phone, _b, ids = two_tenants
    wh_id = client.get("/api/auth/me", headers=a.h).json()["warehouse"]["id"]
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api/admin/"):
            continue
        url = path.replace("{warehouse_id}", wh_id).replace("{photo_id}", ids["photo_id"])
        for method in operations:
            r = client.request(method.upper(), url, headers=a.h, json={"status": "pilot"})
            assert r.status_code == 403, (method, path, r.status_code)
            r = client.request(method.upper(), url, json={"status": "pilot"})
            assert r.status_code == 401, (method, path, r.status_code)


def test_photos_and_flags_stay_in_their_warehouse(client, two_tenants):
    a, a_phone, _b, ids = two_tenants
    # A's phone can't hang a photo on B's flag.
    r = client.post(
        "/api/worker/photos",
        params={"id": str(uuid.uuid4()), "flag_id": ids["flag_event_id"]},
        content=JPEG,
        headers={**a_phone.h, "Content-Type": "image/jpeg"},
    )
    assert r.status_code == 409
    # ...nor replay B's photo id.
    oid = make_order(client, a)["id"]
    flag = {**scan_event(a_phone, oid, ""), "kind": "flag", "reason": "damaged", "scanned_barcode": None}
    sync(client, a_phone, flag)
    r = client.post(
        "/api/worker/photos",
        params={"id": ids["photo_id"], "flag_id": flag["id"]},
        content=JPEG,
        headers={**a_phone.h, "Content-Type": "image/jpeg"},
    )
    assert r.status_code == 400
    for url in ("/api/photos", "/api/dashboard/flags", "/api/reports", "/api/onboarding"):
        r = client.get(url, headers=a.h)
        assert r.status_code == 200, url
        assert ids["photo_id"] not in r.text and "B-SECRET" not in r.text, url
