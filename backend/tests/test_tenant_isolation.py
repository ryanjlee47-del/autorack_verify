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

from autorack.db import get_sessionmaker
from autorack.models import Integration, IntegrationKind

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
    ("PATCH", "/api/integrations/{integration_id}"): {"enabled": False},
    ("PATCH", "/api/products/{product_id}"): {"name": "x"},
    ("POST", "/api/products/{product_id}/barcodes"): {"barcode": "A-NEW-CODE", "pack_qty": 6},
    ("PUT", "/api/products/{product_id}/components"): {"components": []},
    ("POST", "/api/products/{product_id}/substitutes"): {"substitute_id": "00000000-0000-0000-0000-000000000000"},
    ("PATCH", "/api/clients/{client_id}"): {"name": "x"},
    ("PATCH", "/api/inserts/{insert_id}"): {"name": "x"},
    ("PATCH", "/api/shifts/{shift_id}"): {},
    ("POST", "/api/clients/{client_id}/users"): {"email": "portal@example.com"},
}
NO_BODY = {
    ("GET", "/api/clients/{client_id}/accuracy.pdf"),
    # No invitation to B's warehouse: looks exactly like no warehouse at all.
    ("POST", "/api/auth/invitations/{warehouse_id}/accept"),
    ("POST", "/api/auth/invitations/{warehouse_id}/decline"),
    ("GET", "/api/orders/{order_id}"),
    ("POST", "/api/orders/{order_id}/cancel"),
    ("DELETE", "/api/orders/{order_id}/lines/{line_id}"),
    ("GET", "/api/orders/{order_id}/scans"),
    ("GET", "/api/orders/{order_id}/proof"),
    ("GET", "/api/photos/{photo_id}"),
    ("POST", "/api/devices/{device_id}/revoke"),
    ("DELETE", "/api/aliases/{alias_id}"),
    ("GET", "/api/worker/orders/{order_id}"),
    ("DELETE", "/api/integrations/{integration_id}"),
    ("POST", "/api/integrations/{integration_id}/sync"),
    ("POST", "/api/integrations/{integration_id}/products"),
    ("POST", "/api/orders/{order_id}/push-tracking"),
    ("POST", "/api/orders/{order_id}/return"),
    ("POST", "/api/orders/{order_id}/reopen"),
    ("GET", "/api/orders/{order_id}/variance.csv"),
    ("POST", "/api/orders/{order_id}/share"),
    ("DELETE", "/api/orders/{order_id}/share"),
    ("GET", "/api/products/{product_id}"),
    ("DELETE", "/api/products/{product_id}"),
    ("POST", "/api/products/{product_id}/restore"),
    ("POST", "/api/products/{product_id}/image"),
    ("DELETE", "/api/products/{product_id}/image"),
    ("GET", "/api/products/{product_id}/image"),
    ("DELETE", "/api/products/{product_id}/barcodes/{barcode_id}"),
    ("POST", "/api/products/{product_id}/assign-barcode"),
    ("DELETE", "/api/products/{product_id}/substitutes/{substitute_id}"),
    ("GET", "/api/batches/{batch_id}"),
    ("DELETE", "/api/batches/{batch_id}"),
    ("GET", "/api/worker/batches/{batch_id}"),
    ("DELETE", "/api/inserts/{insert_id}"),
    ("POST", "/api/restock/{task_id}/done"),
    ("POST", "/api/restock/{task_id}/cancel"),
    ("POST", "/api/worker/restock/{task_id}/done"),
    ("GET", "/api/orders/{order_id}/claim.pdf"),
    ("GET", "/api/clients/{client_id}/statement"),
    ("GET", "/api/clients/{client_id}/statement.csv"),
    ("GET", "/api/clients/{client_id}/users"),
    ("DELETE", "/api/clients/{client_id}/users/{user_id}"),
}


def _connection_for(client, owner) -> str:
    wh_id = client.get("/api/auth/me", headers=owner.h).json()["warehouse"]["id"]
    with get_sessionmaker()() as db:
        integ = Integration(
            warehouse_id=uuid.UUID(wh_id),
            kind=IntegrationKind.sheet,
            name="B-SECRET-SHEET",
            config={"url": "https://example.com/b.csv"},
            cursor={},
            enabled=True,
            push_tracking=False,
            sync_minutes=15,
            failures=0,
            last_created=0,
            total_created=0,
        )
        db.add(integ)
        db.commit()
        return str(integ.id)


def _catalog_for(client, owner) -> dict[str, str]:
    mk = lambda **f: client.post("/api/products", json=f, headers=owner.h).json()  # noqa: E731
    p = mk(name="B-SECRET-PRODUCT", sku="B-SECRET-SKU", barcode="B-SECRET-UPC")
    sub = mk(name="B-SECRET-SUB", sku="B-SUB", barcode="B-SUB-UPC")
    r = client.post(f"/api/products/{p['id']}/barcodes", json={"barcode": "B-CASE", "pack_qty": 12}, headers=owner.h)
    client.post(f"/api/products/{p['id']}/substitutes", json={"substitute_id": sub["id"]}, headers=owner.h)
    return {"product_id": p["id"], "barcode_id": r.json()["barcodes"][0]["id"], "substitute_id": sub["id"]}


def _floor_for(client, owner, phone, order) -> dict[str, str]:
    c = client.post("/api/clients", json={"name": "B-SECRET-CLIENT"}, headers=owner.h).json()
    i = client.post("/api/inserts", json={"name": "B-SECRET-INSERT"}, headers=owner.h).json()
    client.patch("/api/warehouse", json={"time_clock_enabled": True}, headers=owner.h)
    shift = client.post("/api/worker/clock-in", headers=phone.h).json()["shift"]["id"]
    restock = {**scan_event(phone, order["id"], ""), "kind": "restock", "scanned_barcode": None}
    restock["line_item_id"] = order["lines"][0]["id"]
    sync(client, phone, restock)
    return {"client_id": c["id"], "insert_id": i["id"], "shift_id": shift, "task_id": restock["id"]}


def _batch_for(client, owner) -> str:
    one = make_order(client, owner, [("B-SECRET-B1", 1)], number="B-SECRET-BATCHED-1")
    two = make_order(client, owner, [("B-SECRET-B2", 1)], number="B-SECRET-BATCHED-2")
    r = client.post("/api/batches", json={"order_ids": [one["id"], two["id"]]}, headers=owner.h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


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
        "warehouse_id": client.get("/api/auth/me", headers=b.h).json()["warehouse"]["id"],
        "order_id": b_order["id"],
        "line_id": b_order["lines"][0]["id"],
        "flag_id": client.get(f"/api/orders/{b_order['id']}", headers=b.h).json()["flags"][0]["id"],
        "worker_id": client.get("/api/workers", headers=b.h).json()["workers"][0]["worker_id"],
        "device_id": client.get("/api/devices", headers=b.h).json()[0]["id"],
        "user_id": next(u["id"] for u in client.get("/api/team", headers=b.h).json() if u["role"] == "manager"),
        "alias_id": client.get("/api/aliases", headers=b.h).json()[0]["id"],
        "photo_id": photo_id,
        "flag_event_id": flag["id"],
        "integration_id": _connection_for(client, b),
        **_catalog_for(client, b),
        "batch_id": _batch_for(client, b),
        **_floor_for(client, b, b_phone, b_order),
    }
    return a, a_phone, b, ids


def test_every_id_route_hides_other_tenants(app, client, two_tenants):
    a, a_phone, _b, ids = two_tenants
    checked = 0
    # The OpenAPI schema is the public, complete list of routes and methods.
    for path, operations in app.openapi()["paths"].items():
        if "{" not in path or path.startswith("/api/admin/"):
            continue  # admin routes cross tenants by design; see test_admin_routes_need_operator
        if path.startswith(("/api/inbound/", "/api/public/")):
            continue  # authenticated by the secret in the URL itself; see test_integrations, test_share_monthly
        if path.startswith("/api/portal/"):
            continue  # client logins only, scoped to one client; see test_client_portal
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
        "/api/integrations",
        "/api/products",
        "/api/products?q=SECRET",
        "/api/batches",
        "/api/clients",
        "/api/inserts",
        "/api/restock?status=all",
        "/api/shifts",
        "/api/exports/timesheet.csv",
        "/api/billing/clients",
        "/api/search?q=SECRET",
        "/api/search?q=B-SECRET-BARCODE",
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
    assert client.get("/api/worker/restock", headers=a_phone.h).json()["tasks"] == []
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
        url = path.replace("{warehouse_id}", wh_id).replace("{photo_id}", ids["photo_id"]).replace("{error_id}", "1")
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
