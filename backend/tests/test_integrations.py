"""Store connections, spreadsheet links, the import address, and tracking
going back to the store. Stores are faked with httpx.MockTransport: these
tests pin down what we send and how we read the answers."""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from conftest import scan, signup, sync, worker_on_phone
from test_floor_features import ship_event

from autorack.config import get_settings
from autorack.models import Integration, Order, utcnow
from autorack.services import email, integrations, jobs, secretbox, stores


def _png() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (400, 400), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


class FakeStores:
    """One fake for every vendor, routed by host."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.shopify_orders: list[dict[str, Any]] = []
        self.shopify_fulfillment_orders = [{"id": "gid://shopify/FulfillmentOrder/9", "status": "OPEN"}]
        self.shipstation_orders: list[dict[str, Any]] = []
        self.woo_orders: list[dict[str, Any]] = []
        self.sheet = b"order_number,barcode,quantity\nS-1,012345678905,2\n"
        self.fail_with: int | None = None
        self.shopify_variants: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with:
            return httpx.Response(self.fail_with, text="nope")
        host, path = request.url.host, request.url.path
        if host.endswith("myshopify.com"):
            if request.headers.get("X-Shopify-Access-Token") != "shpat_good":
                return httpx.Response(401, json={"errors": "[API] Invalid API key or access token"})
            q = json.loads(request.content)["query"]
            if "shop { name }" in q:
                return httpx.Response(200, json={"data": {"shop": {"name": "Dockside Goods"}}})
            if "orders(" in q:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "orders": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": self.shopify_orders,
                            }
                        }
                    },
                )
            if "productVariants" in q:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "productVariants": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": self.shopify_variants,
                            }
                        }
                    },
                )
            if "fulfillmentOrders" in q:
                return httpx.Response(
                    200, json={"data": {"order": {"fulfillmentOrders": {"nodes": self.shopify_fulfillment_orders}}}}
                )
            if "fulfillmentCreate" in q:
                return httpx.Response(
                    200, json={"data": {"fulfillmentCreate": {"fulfillment": {"id": "f1"}, "userErrors": []}}}
                )
        if host == "ssapi.shipstation.com":
            if request.headers.get("authorization") != "Basic " + base64.b64encode(b"key:secret").decode():
                return httpx.Response(401)
            if path == "/stores":
                return httpx.Response(200, json=[{"storeId": 1}])
            if path == "/orders":
                return httpx.Response(200, json={"orders": self.shipstation_orders, "pages": 1})
            if path == "/orders/markasshipped":
                return httpx.Response(200, json={"orderId": 1})
        if host == "shop.example.com":
            if path == "/wp-json/wc/v3/orders":
                return httpx.Response(200, json=self.woo_orders)
            if path == "/wp-json/wc/v3/products":
                return httpx.Response(200, json=[{"id": 11, "global_unique_id": "0036000291452"}])
            if path.endswith("/variations"):
                return httpx.Response(200, json=[{"id": 21, "global_unique_id": "012345678905"}])
            if path.endswith("/notes") or request.method == "PUT":
                return httpx.Response(200, json={})
        if host == "cdn.shopify.com":
            return httpx.Response(200, content=_png(), headers={"content-type": "image/png"})
        if host == "docs.google.com":
            return httpx.Response(200, content=self.sheet, headers={"content-type": "text/csv"})
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch):
    f = FakeStores()
    monkeypatch.setattr(stores, "TRANSPORT", httpx.MockTransport(f))
    monkeypatch.setattr(
        stores, "_resolve", lambda host: ["10.0.0.5"] if host.startswith("internal") else ["93.184.216.34"]
    )
    return f


def shopify_order(gid: str, name: str, lines: list[tuple[str | None, str | None, int]]) -> dict[str, Any]:
    return {
        "id": gid,
        "name": name,
        "cancelledAt": None,
        "customer": {"displayName": "Jane Buyer"},
        "shippingAddress": {"name": "Jane Buyer", "company": None},
        "lineItems": {
            "nodes": [
                {
                    "name": f"Item {i}",
                    "sku": sku,
                    "unfulfilledQuantity": qty,
                    "requiresShipping": True,
                    "variant": {"barcode": barcode, "sku": sku},
                }
                for i, (barcode, sku, qty) in enumerate(lines)
            ]
        },
    }


def connect_shopify(client, owner, **extra):
    return client.post(
        "/api/integrations",
        json={"kind": "shopify", "shop": "dockside", "token": "shpat_good", **extra},
        headers=owner.h,
    )


def orders_of(client, owner):
    return client.get("/api/orders", headers=owner.h).json()["orders"]


def test_shopify_connect_pulls_orders_and_pushes_tracking(client, fake, db):
    owner = signup(client)
    fake.shopify_orders = [
        shopify_order("gid://shopify/Order/1", "#1001", [("012345678905", "WID", 2), (None, "TAPE-48", 1)]),
        shopify_order("gid://shopify/Order/2", "#1002", [(None, None, 1)]),  # nothing scannable
    ]
    r = connect_shopify(client, owner)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["connection"]["name"] == "Dockside Goods"
    assert body["connection"]["shop"] == "dockside.myshopify.com"
    assert body["sync"]["created"] == 1
    assert "token" not in json.dumps(body["connection"])
    # Credentials are stored encrypted.
    integ = db.query(Integration).one()
    assert "shpat_good" not in (integ.secret or "")
    assert secretbox.unseal(integ.secret) == {"token": "shpat_good"}

    (o,) = orders_of(client, owner)
    assert o["external_order_number"] == "1001" and o["source"] == "shopify"
    detail = client.get(f"/api/orders/{o['id']}", headers=owner.h).json()
    # SKU stands in for a missing barcode.
    assert {li["expected_barcode"] for li in detail["lines"]} == {"012345678905", "TAPE-48"}

    # Syncing again doesn't duplicate; a cancelled order doesn't come back.
    client.post(f"/api/orders/{o['id']}/cancel", headers=owner.h)
    db.query(Integration).update({"last_sync_at": None})
    db.commit()
    r = client.post(f"/api/integrations/{integ.id}/sync", headers=owner.h)
    assert r.json()["sync"]["created"] == 0
    assert len(orders_of(client, owner)) == 1

    # A new order arrives, is picked, and shipped: tracking goes to Shopify.
    fake.shopify_orders.append(shopify_order("gid://shopify/Order/3", "#1003", [("036000291452", "X", 1)]))
    db.query(Integration).update({"last_sync_at": utcnow() - timedelta(hours=1)})
    db.commit()
    assert integrations.run_store_sync(db, utcnow()) == 1
    new = next(x for x in orders_of(client, owner) if x["external_order_number"] == "1003")
    phone = worker_on_phone(client, owner)
    scan(client, phone, new["id"], "036000291452")
    out = sync(client, phone, ship_event(phone, new["id"], "1Z999AA10123456784"))
    assert out["events"][0]["result"] == "shipped"
    assert client.get(f"/api/orders/{new['id']}", headers=owner.h).json()["tracking_push"]["status"] == ("pending")
    assert integrations.run_tracking_push(db, utcnow()) == 1
    sent = json.loads(fake.requests[-1].content)
    assert "fulfillmentCreate" in sent["query"]
    assert sent["variables"]["f"]["trackingInfo"] == {"number": "1Z999AA10123456784", "company": "UPS"}
    assert sent["variables"]["f"]["notifyCustomer"] is True
    state = client.get(f"/api/orders/{new['id']}", headers=owner.h).json()["tracking_push"]
    assert state["status"] == "done"


def test_bad_credentials_are_refused_before_saving(client, fake, db):
    owner = signup(client)
    r = connect_shopify(client, owner, token="shpat_wrong")
    assert r.status_code == 400
    assert "refused the credentials" in r.json()["detail"]["message"]
    r = client.post("/api/integrations", json={"kind": "shopify", "shop": "evil.com", "token": "x"}, headers=owner.h)
    assert r.status_code == 400 and "myshopify.com" in r.json()["detail"]["message"]
    assert db.query(Integration).count() == 0


def test_only_owners_connect(client, fake):
    owner = signup(client)
    client.post("/api/team", json={"email": "mgr@example.com"}, headers=owner.h)
    from conftest import google_login

    tok = google_login(client, "mgr@example.com")
    r = client.post(
        "/api/integrations",
        json={"kind": "shopify", "shop": "dockside", "token": "shpat_good"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 403


def test_shipstation_pull_and_mark_shipped(client, fake, db):
    owner = signup(client)
    fake.shipstation_orders = [
        {
            "orderId": 555,
            "orderNumber": "SS-9",
            "shipTo": {"name": "Bob", "company": "Northside Supply"},
            "items": [
                {"sku": "WID", "name": "Widget", "quantity": 3, "upc": "012345678905"},
                {"sku": None, "name": "Discount", "quantity": 1, "adjustment": True},
            ],
        }
    ]
    r = client.post(
        "/api/integrations", json={"kind": "shipstation", "api_key": "key", "api_secret": "secret"}, headers=owner.h
    )
    assert r.status_code == 201, r.text
    assert r.json()["sync"]["created"] == 1
    (o,) = orders_of(client, owner)
    assert o["customer"] == "Northside Supply"
    phone = worker_on_phone(client, owner)
    for _ in range(3):
        scan(client, phone, o["id"], "012345678905")
    sync(client, phone, ship_event(phone, o["id"], "9400111899223197428490"))
    integrations.run_tracking_push(db, utcnow())
    req = fake.requests[-1]
    assert req.url.path == "/orders/markasshipped"
    sent = json.loads(req.content)
    assert sent["orderId"] == 555 and sent["carrierCode"] == "usps" and sent["notifyCustomer"] is True


def test_woocommerce_uses_gtin_then_sku(client, fake):
    owner = signup(client)
    fake.woo_orders = [
        {
            "id": 77,
            "number": "4077",
            "shipping": {"first_name": "Ann", "last_name": "Lee", "company": ""},
            "billing": {},
            "line_items": [
                {"name": "Tape", "product_id": 11, "variation_id": 0, "quantity": 2, "sku": "TAPE"},
                {"name": "Widget blue", "product_id": 12, "variation_id": 21, "quantity": 1, "sku": "WID-B"},
                {"name": "Gift card", "product_id": 13, "variation_id": 0, "quantity": 1, "sku": ""},
            ],
        }
    ]
    r = client.post(
        "/api/integrations",
        json={"kind": "woocommerce", "store_url": "shop.example.com", "consumer_key": "ck", "consumer_secret": "cs"},
        headers=owner.h,
    )
    assert r.status_code == 201, r.text
    sync_result = r.json()["sync"]
    assert sync_result["created"] == 1
    assert any("without a barcode or SKU" in w for w in sync_result["warnings"])
    (o,) = orders_of(client, owner)
    assert o["customer"] == "Ann Lee"
    lines = client.get(f"/api/orders/{o['id']}", headers=owner.h).json()["lines"]
    assert {li["expected_barcode"] for li in lines} == {"0036000291452", "012345678905"}


def test_private_addresses_are_refused(client, fake):
    owner = signup(client)
    for url in ("http://docs.example.com/x.csv", "https://internal.example.com/x.csv", "https://localhost/x"):
        r = client.post("/api/integrations", json={"kind": "sheet", "url": url}, headers=owner.h)
        assert r.status_code == 400, url
    r = client.post(
        "/api/integrations",
        json={
            "kind": "woocommerce",
            "store_url": "https://internal.shop.com",
            "consumer_key": "a",
            "consumer_secret": "b",
        },
        headers=owner.h,
    )
    assert r.status_code == 400


def test_redirects_to_private_addresses_are_refused(fake, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.example.com":
            return httpx.Response(302, headers={"location": "https://internal.example.com/secret"})
        return httpx.Response(200, text="secret")

    monkeypatch.setattr(stores, "TRANSPORT", httpx.MockTransport(handler))
    with pytest.raises(stores.StoreError, match="private network"):
        stores.fetch_public("https://public.example.com/x.csv", "test")


def test_google_sheet_link_imports_on_schedule(client, fake, db):
    owner = signup(client)
    assert stores.sheet_csv_url("https://docs.google.com/spreadsheets/d/abc_123/edit#gid=42") == (
        "https://docs.google.com/spreadsheets/d/abc_123/export?format=csv&gid=42"
    )
    r = client.post(
        "/api/integrations",
        json={"kind": "sheet", "url": "https://docs.google.com/spreadsheets/d/abc_123/edit#gid=0"},
        headers=owner.h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["sync"]["created"] == 1
    assert r.json()["connection"]["push_tracking"] is False
    # Rows added to the sheet later arrive on the next pull; old ones don't repeat.
    fake.sheet += b"S-2,036000291452,1\n"
    db.query(Integration).update({"last_sync_at": utcnow() - timedelta(hours=1)})
    db.commit()
    integrations.run_store_sync(db, utcnow())
    assert sorted(o["external_order_number"] for o in orders_of(client, owner)) == ["S-1", "S-2"]
    imports = client.get("/api/imports", headers=owner.h).json()
    assert len(imports) == 2  # empty re-reads leave no history


def test_failing_store_backs_off_and_emails_owners_once(client, fake, db):
    owner = signup(client)
    assert connect_shopify(client, owner).status_code == 201
    fake.fail_with = 401
    email.outbox.clear()
    integ = db.query(Integration).one()
    now = utcnow()
    for i in range(5):
        db.refresh(integ)
        integ.last_sync_at = now - timedelta(days=1)
        db.commit()
        integrations.sync_one(db, integ, now + timedelta(minutes=i))
    db.refresh(integ)
    assert integ.failures == 5 and "refused the credentials" in integ.last_error
    assert integrations.next_due(integ) - integ.last_sync_at == timedelta(minutes=120)
    alerts = [m for m in email.outbox if "aren't reaching Autorack" in m.subject]
    assert len(alerts) == 1 and alerts[0].to == owner.email
    listed = client.get("/api/integrations", headers=owner.h).json()["connections"][0]
    assert listed["state"] == "failing"
    # Fixed: it recovers.
    fake.fail_with = None
    integrations.sync_one(db, integ, now + timedelta(hours=3))
    db.refresh(integ)
    assert integ.failures == 0 and integ.last_error is None


def test_disconnect_forgets_credentials_and_skips_pushes(client, fake, db):
    owner = signup(client)
    fake.shopify_orders = [shopify_order("gid://shopify/Order/1", "#1", [("012345678905", "W", 1)])]
    integ_id = connect_shopify(client, owner).json()["connection"]["id"]
    (o,) = orders_of(client, owner)
    phone = worker_on_phone(client, owner)
    scan(client, phone, o["id"], "012345678905")
    sync(client, phone, ship_event(phone, o["id"], "1Z999AA10123456784"))
    assert client.delete(f"/api/integrations/{integ_id}", headers=owner.h).status_code == 204
    integ = db.get(Integration, integ_id)
    assert integ.secret is None and integ.deleted_at
    assert db.query(Order).one().tracking_push_status == "skipped"
    assert client.get("/api/integrations", headers=owner.h).json()["connections"] == []
    assert len(orders_of(client, owner)) == 1  # orders stay


def test_push_failure_retries_then_manual_retry(client, fake, db):
    owner = signup(client)
    fake.shopify_orders = [shopify_order("gid://shopify/Order/1", "#1", [("012345678905", "W", 1)])]
    connect_shopify(client, owner)
    (o,) = orders_of(client, owner)
    phone = worker_on_phone(client, owner)
    scan(client, phone, o["id"], "012345678905")
    sync(client, phone, ship_event(phone, o["id"], "1Z999AA10123456784"))
    fake.fail_with = 503
    now = utcnow()
    integrations.run_tracking_push(db, now)
    order = db.query(Order).one()
    db.refresh(order)
    assert order.tracking_push_status == "failed" and order.tracking_push_attempts == 1
    assert integrations.run_tracking_push(db, now + timedelta(minutes=1)) == 0  # waiting to retry
    fake.fail_with = None
    r = client.post(f"/api/orders/{o['id']}/push-tracking", headers=owner.h)
    assert r.json()["ok"] is True
    # Already fulfilled in Shopify (by hand) counts as done.
    fake.shopify_fulfillment_orders = [{"id": "x", "status": "CLOSED"}]
    db.refresh(order)
    order.tracking_push_status = "pending"
    db.commit()
    assert integrations.push_one(db, order)["result"] == "already fulfilled in Shopify"


def test_drop_url_imports_a_csv(client, db):
    owner = signup(client)
    addr = client.post("/api/integrations/import-address", headers=owner.h).json()
    assert addr["drop_url"].startswith("https://app.autorack.test/api/inbound/drop/")
    token = addr["drop_url"].rsplit("/", 1)[1]
    csv = b"order_number,barcode,quantity\nD-1,012345678905,1\nD-2,,1\n"
    r = client.post(f"/api/inbound/drop/{token}", files={"file": ("picks.csv", csv, "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json()["orders_created"] == 1
    # Raw body works too; repeats are skipped.
    r = client.post(f"/api/inbound/drop/{token}", content=csv, headers={"Content-Type": "text/csv"})
    assert r.json()["orders_created"] == 0 and r.json()["orders_skipped"] == 1
    assert orders_of(client, owner)[0]["source"] == "drop"
    assert client.post("/api/inbound/drop/" + "0" * 32, content=csv).status_code == 404
    # Rotating kills the old address.
    new = client.post("/api/integrations/import-address/rotate", headers=owner.h).json()
    assert new["drop_url"] != addr["drop_url"]
    assert client.post(f"/api/inbound/drop/{token}", content=csv).status_code == 404


def test_inbound_email_imports_attachments(client, db, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "inbound_email_address", "abc+{token}@inbound.example.com")
    monkeypatch.setattr(s, "inbound_email_secret", "inbound-secret-123456")
    owner = signup(client)
    addr = client.post("/api/integrations/import-address", headers=owner.h).json()
    assert addr["email"].startswith("abc+") and addr["email"].endswith("@inbound.example.com")
    csv = b"order_number,barcode,quantity\nE-1,012345678905,3\n"
    postmark = {
        "From": f"Owner <{owner.email}>",
        "To": addr["email"],
        "OriginalRecipient": addr["email"],
        "Subject": "picks",
        "Attachments": [{"Name": "today.csv", "ContentType": "text/csv", "Content": base64.b64encode(csv).decode()}],
    }
    assert client.post("/api/inbound/email?key=wrong", json=postmark).status_code == 404
    email.outbox.clear()
    r = client.post("/api/inbound/email?key=inbound-secret-123456", json=postmark)
    assert r.status_code == 200 and r.json()["ok"], r.text
    (o,) = orders_of(client, owner)
    assert o["external_order_number"] == "E-1" and o["source"] == "email"
    assert [m.to for m in email.outbox] == [owner.email]  # the sender hears back
    # Mailgun-style multipart from a stranger: imported, but no reply sent.
    email.outbox.clear()
    r = client.post(
        "/api/inbound/email?key=inbound-secret-123456",
        data={"recipient": addr["email"], "from": "erp@vendor.example.com"},
        files={"attachment-1": ("more.csv", b"order_number,barcode\nE-2,036000291452\n", "text/csv")},
    )
    assert r.json()["ok"]
    assert len(orders_of(client, owner)) == 2
    assert email.outbox == []
    # Unknown address: accepted (no retries) but nothing happens.
    r = client.post(
        "/api/inbound/email?key=inbound-secret-123456", json={**postmark, "To": "x@y.z", "OriginalRecipient": ""}
    )
    assert r.json() == {"ok": False, "reason": "unknown address"}


def test_jobs_runner_includes_sync_and_push(db):
    out = jobs.run_all(db, utcnow())
    assert out["store_sync"] == 0 and out["tracking_push"] == 0


def test_store_products_come_into_the_catalog_with_pictures(client, fake):
    owner = signup(client)
    client.post("/api/products", json={"name": "Old name", "sku": "WID"}, headers=owner.h)  # matched by SKU
    fake.shopify_variants = [
        {
            "id": "gid://shopify/ProductVariant/1",
            "sku": "WID",
            "barcode": "012345678905",
            "title": "Blue",
            "image": {"url": "https://cdn.shopify.com/blue.png"},
            "product": {"title": "Widget", "featuredImage": None},
        },
        {
            "id": "gid://shopify/ProductVariant/2",
            "sku": "TAPE",
            "barcode": "012345678905",  # clashes: kept off, reported
            "title": "Default Title",
            "image": None,
            "product": {"title": "Tape", "featuredImage": {"url": "https://cdn.shopify.com/tape.png"}},
        },
        {"id": "gid://shopify/ProductVariant/3", "sku": "", "barcode": "", "title": "x", "product": {"title": "Gift"}},
    ]
    r = connect_shopify(client, owner)
    res = r.json()["sync"]["products"]
    assert (res["found"], res["created"], res["updated"], res["images"]) == (3, 1, 1, 2), res
    assert any("already on another product" in w for w in res["warnings"])
    listed = {p["sku"]: p for p in client.get("/api/products", headers=owner.h).json()["products"]}
    assert listed["WID"]["name"] == "Widget - Blue" and listed["WID"]["barcode"] == "012345678905"
    assert listed["WID"]["thumb"].startswith("data:image/jpeg") and listed["TAPE"]["barcode"] is None
    assert listed["WID"]["source"] == "manual" and listed["TAPE"]["source"] == "shopify"
    # Again: nothing new, no duplicates.
    integ_id = client.get("/api/integrations", headers=owner.h).json()["connections"][0]["id"]
    again = client.post(f"/api/integrations/{integ_id}/products", headers=owner.h).json()
    assert (again["created"], again["updated"]) == (0, 2)
    assert len(client.get("/api/products", headers=owner.h).json()["products"]) == 2
