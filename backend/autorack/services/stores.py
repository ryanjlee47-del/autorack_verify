"""Talking to the places orders come from: Shopify, ShipStation, WooCommerce,
and a spreadsheet link (Google Sheets or any CSV URL).

Each connector does three things:

* `check()`   -- prove the credentials work (called before saving them);
* `fetch()`   -- the orders waiting to be picked, as `StoreOrder`s;
* `push()`    -- tell the store an order shipped, with its tracking number.

The sync job (services/integrations.py) decides what's new; connectors only
translate. Every failure the owner can fix is raised as `StoreError` with a
message written for them ("Shopify refused the access token..."), which is
what the Connections page shows.

Every URL fetched here is either a fixed vendor API host or checked by
`check_public_url` (https, and resolves only to public addresses), so a
pasted link can't make the server call into its own network. That check
alone can be raced (DNS rebinding: public when checked, 169.254.169.254 a
moment later), so every connection also goes through `PublicOnlyBackend`,
which resolves the name itself and connects only to the public address it
just checked. Responses are capped at MAX_BODY while they download.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlparse

import httpcore
import httpx

from ..models import IntegrationKind, Order, utcnow
from . import orders as order_svc

SHOPIFY_API_VERSION = "2025-07"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_PAGES = 10
LOOKBACK_DAYS = 30
MAX_BODY = 20 * 1024 * 1024

# Tests swap this for httpx.MockTransport.
TRANSPORT: httpx.BaseTransport | None = None


def is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class PublicOnlyBackend(httpcore.SyncBackend):
    """Connects only to public addresses, checked at connect time. TLS still
    verifies the certificate against the hostname (httpcore passes it as the
    server name), so pinning the address doesn't weaken HTTPS."""

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            raise httpcore.ConnectError(f"can't resolve {host}") from exc
        addrs = [str(i[4][0]) for i in infos]
        if not addrs or not all(is_public_ip(a) for a in addrs):
            raise httpcore.ConnectError(f"{host} resolves to a private address")
        return super().connect_tcp(addrs[0], port, timeout, local_address, socket_options)


class CappedStream(httpx.SyncByteStream):
    def __init__(self, inner: httpx.SyncByteStream, limit: int) -> None:
        self.inner, self.limit = inner, limit

    def __iter__(self) -> Any:
        seen = 0
        for chunk in self.inner:
            seen += len(chunk)
            if seen > self.limit:
                raise StoreError("The answer was larger than 20 MB, so it was ignored.", retry=False)
            yield chunk

    def close(self) -> None:
        self.inner.close()


class SafeTransport(httpx.BaseTransport):
    """Public addresses only, and no response bigger than MAX_BODY."""

    def __init__(self) -> None:
        self.inner = httpx.HTTPTransport(trust_env=False)
        self.inner._pool = httpcore.ConnectionPool(
            ssl_context=self.inner._pool._ssl_context, network_backend=PublicOnlyBackend()
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        resp = self.inner.handle_request(request)
        declared = resp.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY:
            resp.close()
            raise StoreError("The answer was larger than 20 MB, so it was ignored.", retry=False)
        assert isinstance(resp.stream, httpx.SyncByteStream)
        return httpx.Response(
            resp.status_code,
            headers=resp.headers,
            stream=CappedStream(resp.stream, MAX_BODY),
            extensions=resp.extensions,
            request=request,
        )

    def close(self) -> None:
        self.inner.close()


class StoreError(Exception):
    """Something the owner can act on. `retry` = worth trying again later."""

    def __init__(self, message: str, *, retry: bool = True) -> None:
        super().__init__(message)
        self.message = message
        self.retry = retry


@dataclass
class StoreProduct:
    external_id: str
    name: str
    sku: str | None
    barcode: str | None
    image_url: str | None = None
    weight_grams: int | None = None
    location: str | None = None


@dataclass
class StoreOrder:
    store_order_id: str
    number: str
    customer: str | None
    lines: list[order_svc.LineInput]
    skipped_lines: list[str] = field(default_factory=list)
    rush: bool = False
    ship_by: date | None = None


EXPEDITED = re.compile(r"overnight|next[ -]?day|express|priority overnight|same[ -]?day|1[ -]?day", re.I)


def client(**kw: Any) -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT,
        transport=TRANSPORT or SafeTransport(),
        headers={"User-Agent": "Autorack/1.0 (+order sync)"},
        **kw,
    )


def _line(barcode: str | None, sku: str | None, qty: int, name: str | None) -> order_svc.LineInput | None:
    """A store line becomes a pick line. The barcode is what the worker will
    scan; stores without one on the product fall back to the SKU (many
    warehouses label bins and products with SKU barcodes)."""
    code = (barcode or "").strip() or (sku or "").strip()
    if not code or qty < 1 or len(code) > 200:
        return None
    return order_svc.LineInput(
        barcode=code,
        quantity=min(qty, 100_000),
        sku=order_svc.clean(sku, 100),
        description=order_svc.clean(name, 500),
    )


def _raise_for(resp: httpx.Response, who: str) -> None:
    if resp.status_code in (401, 403):
        raise StoreError(f"{who} refused the credentials. Check the key and its permissions, then reconnect.")
    if resp.status_code == 404:
        raise StoreError(f"{who} says that address doesn't exist. Check the store address.", retry=False)
    if resp.status_code == 429:
        raise StoreError(f"{who} asked us to slow down; will try again shortly.")
    if resp.status_code >= 500:
        raise StoreError(f"{who} is having trouble right now ({resp.status_code}); will try again shortly.")
    if resp.status_code >= 400:
        raise StoreError(f"{who} rejected the request ({resp.status_code}): {resp.text[:200]}")


def _get(c: httpx.Client, who: str, url: str, **kw: Any) -> httpx.Response:
    try:
        resp = c.get(url, **kw)
    except httpx.TimeoutException as exc:
        raise StoreError(f"{who} took too long to answer; will try again shortly.") from exc
    except httpx.HTTPError as exc:
        raise StoreError(f"Couldn't reach {who}. Check the address, or try again in a few minutes.") from exc
    _raise_for(resp, who)
    return resp


def _send(c: httpx.Client, who: str, method: str, url: str, **kw: Any) -> httpx.Response:
    try:
        resp = c.request(method, url, **kw)
    except httpx.TimeoutException as exc:
        raise StoreError(f"{who} took too long to answer; will try again shortly.") from exc
    except httpx.HTTPError as exc:
        raise StoreError(f"Couldn't reach {who}. Check the address, or try again in a few minutes.") from exc
    _raise_for(resp, who)
    return resp


# ---------------------------------------------------------------------------
# Public URLs only
# ---------------------------------------------------------------------------


def _resolve(host: str) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)]


def check_public_url(url: str) -> str:
    """https, a real hostname, and every address it resolves to is public."""
    url = (url or "").strip()
    p = urlparse(url)
    if p.scheme != "https":
        raise StoreError("The address must start with https://", retry=False)
    host = (p.hostname or "").lower()
    if not host or "." not in host or host.endswith((".local", ".internal", ".localhost")):
        raise StoreError("That address doesn't look like a public website.", retry=False)
    if p.username or p.password:
        raise StoreError("Leave the username and password out of the address.", retry=False)
    try:
        addrs = _resolve(host)
    except OSError as exc:
        raise StoreError(f"Couldn't find {host}. Check the address.") from exc
    if not all(is_public_ip(a) for a in addrs):
        raise StoreError("That address points inside a private network; Autorack can't fetch it.", retry=False)
    return url


def fetch_public(url: str, who: str, *, auth: tuple[str, str] | None = None) -> httpx.Response:
    """GET a user-supplied URL, re-checking every redirect hop."""
    with client(follow_redirects=False) as c:
        for _ in range(6):
            check_public_url(url)
            resp = _get(c, who, url, auth=auth)
            if resp.is_redirect and resp.headers.get("location"):
                url = urljoin(url, resp.headers["location"])
                auth = None  # never forward credentials to another host
                continue
            if len(resp.content) > MAX_BODY:
                raise StoreError(f"{who}: the file is larger than 20 MB.", retry=False)
            return resp
    raise StoreError(f"{who} redirected too many times.", retry=False)


# ---------------------------------------------------------------------------
# Shopify (Admin GraphQL API, custom-app access token)
# ---------------------------------------------------------------------------

SHOPIFY_CARRIERS = {"UPS": "UPS", "USPS": "USPS", "FedEx": "FedEx", "DHL": "DHL Express", "Amazon": "Amazon Logistics"}

SHOPIFY_ORDERS = """
query Orders($after: String, $q: String) {
  orders(first: 50, after: $after, query: $q, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id name cancelledAt
      customer { displayName }
      shippingAddress { name company }
      lineItems(first: 100) {
        nodes { name sku unfulfilledQuantity requiresShipping variant { barcode sku } }
      }
    }
  }
}
"""

SHOPIFY_PRODUCTS = """
query Variants($after: String) {
  productVariants(first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id sku barcode title
      image { url }
      product { title featuredImage { url } }
    }
  }
}
"""

SHOPIFY_FULFILLMENT_ORDERS = """
query FO($id: ID!) {
  order(id: $id) { fulfillmentOrders(first: 20) { nodes { id status } } }
}
"""

SHOPIFY_FULFILL = """
mutation Fulfill($f: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $f) { fulfillment { id } userErrors { field message } }
}
"""


def shopify_domain(raw: str) -> str:
    s = (raw or "").strip().lower()
    s = re.sub(r"^https?://", "", s).split("/")[0]
    if s and "." not in s:
        s = f"{s}.myshopify.com"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", s):
        raise StoreError(
            "Enter your store's myshopify.com address, e.g. dockside.myshopify.com "
            "(Shopify admin → Settings → Domains).",
            retry=False,
        )
    return s


class Shopify:
    who = "Shopify"

    def __init__(self, config: dict[str, Any], secret: dict[str, Any]) -> None:
        self.shop = shopify_domain(config.get("shop", ""))
        self.token = str(secret.get("token") or "")
        if not self.token:
            raise StoreError("Paste the Admin API access token from your Shopify custom app.", retry=False)

    def _gql(self, c: httpx.Client, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = _send(
            c,
            self.who,
            "POST",
            f"https://{self.shop}/admin/api/{SHOPIFY_API_VERSION}/graphql.json",
            json={"query": query, "variables": variables or {}},
            headers={"X-Shopify-Access-Token": self.token},
        )
        body = resp.json()
        errs = body.get("errors")
        if errs:
            msg = errs[0].get("message", "") if isinstance(errs, list) else str(errs)
            if "access" in msg.lower() or "scope" in msg.lower():
                raise StoreError(f"Shopify: the app is missing a permission ({msg[:160]}).", retry=False)
            if "throttled" in msg.lower():
                raise StoreError("Shopify asked us to slow down; will try again shortly.")
            raise StoreError(f"Shopify: {msg[:200]}")
        return dict(body.get("data") or {})

    def check(self) -> str:
        with client() as c:
            data = self._gql(c, "{ shop { name } }")
        return str((data.get("shop") or {}).get("name") or self.shop)

    def fetch(self, since: datetime) -> list[StoreOrder]:
        q = f"status:open fulfillment_status:unfulfilled created_at:>='{since.strftime('%Y-%m-%dT%H:%M:%SZ')}'"
        out: list[StoreOrder] = []
        after = None
        with client() as c:
            for _ in range(MAX_PAGES):
                data = self._gql(c, SHOPIFY_ORDERS, {"after": after, "q": q})
                conn = data.get("orders") or {}
                for n in conn.get("nodes") or []:
                    if n.get("cancelledAt"):
                        continue
                    so = StoreOrder(
                        store_order_id=str(n["id"]),
                        number=str(n.get("name") or n["id"]).lstrip("#") or str(n["id"]),
                        customer=(
                            (n.get("shippingAddress") or {}).get("company")
                            or (n.get("shippingAddress") or {}).get("name")
                            or (n.get("customer") or {}).get("displayName")
                        ),
                        lines=[],
                    )
                    for li in (n.get("lineItems") or {}).get("nodes") or []:
                        if li.get("requiresShipping") is False:
                            continue
                        variant = li.get("variant") or {}
                        line = _line(
                            variant.get("barcode"),
                            li.get("sku") or variant.get("sku"),
                            int(li.get("unfulfilledQuantity") or 0),
                            li.get("name"),
                        )
                        if line:
                            so.lines.append(line)
                        elif int(li.get("unfulfilledQuantity") or 0) > 0:
                            so.skipped_lines.append(str(li.get("name") or "item"))
                    out.append(so)
                page = conn.get("pageInfo") or {}
                if not page.get("hasNextPage"):
                    break
                after = page.get("endCursor")
        return out

    def products(self) -> list[StoreProduct]:
        out: list[StoreProduct] = []
        after = None
        with client() as c:
            for _ in range(50):  # up to 5,000 variants per pull
                data = self._gql(c, SHOPIFY_PRODUCTS, {"after": after})
                conn = data.get("productVariants") or {}
                for v in conn.get("nodes") or []:
                    prod = v.get("product") or {}
                    title = prod.get("title") or ""
                    if v.get("title") and v["title"] != "Default Title":
                        title = f"{title} - {v['title']}"
                    out.append(
                        StoreProduct(
                            external_id=str(v["id"]),
                            name=title[:500] or (v.get("sku") or "Product"),
                            sku=(v.get("sku") or "").strip()[:100] or None,
                            barcode=(v.get("barcode") or "").strip()[:200] or None,
                            image_url=(
                                (v.get("image") or {}).get("url") or (prod.get("featuredImage") or {}).get("url")
                            ),
                        )
                    )
                page = conn.get("pageInfo") or {}
                if not page.get("hasNextPage"):
                    break
                after = page.get("endCursor")
        return out

    def push(self, order: Order) -> str:
        with client() as c:
            data = self._gql(c, SHOPIFY_FULFILLMENT_ORDERS, {"id": order.store_order_id})
            fos = ((data.get("order") or {}).get("fulfillmentOrders") or {}).get("nodes") or []
            open_ids = [f["id"] for f in fos if f.get("status") in ("OPEN", "IN_PROGRESS", "SCHEDULED")]
            if not open_ids:
                return "already fulfilled in Shopify"
            fulfillment: dict[str, Any] = {
                "lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": i} for i in open_ids],
                "notifyCustomer": True,
                "trackingInfo": {"number": order.tracking_number},
            }
            if order.carrier in SHOPIFY_CARRIERS:
                fulfillment["trackingInfo"]["company"] = SHOPIFY_CARRIERS[order.carrier]
            data = self._gql(c, SHOPIFY_FULFILL, {"f": fulfillment})
            errs = (data.get("fulfillmentCreate") or {}).get("userErrors") or []
            if errs:
                raise StoreError(f"Shopify: {errs[0].get('message', 'could not fulfill')[:200]}", retry=False)
        return "fulfilled in Shopify; customer notified"


# ---------------------------------------------------------------------------
# ShipStation (v1 API, key + secret)
# ---------------------------------------------------------------------------

SHIPSTATION_BASE = "https://ssapi.shipstation.com"
SHIPSTATION_CARRIERS = {"UPS": "ups", "USPS": "usps", "FedEx": "fedex", "DHL": "dhl_express", "Amazon": "amazon"}


class ShipStation:
    who = "ShipStation"

    def __init__(self, config: dict[str, Any], secret: dict[str, Any]) -> None:
        self.auth = (str(secret.get("api_key") or ""), str(secret.get("api_secret") or ""))
        if not all(self.auth):
            raise StoreError(
                "Paste both the API key and the API secret (ShipStation → Settings → Account → API Settings).",
                retry=False,
            )

    def check(self) -> str:
        with client() as c:
            stores = _get(c, self.who, f"{SHIPSTATION_BASE}/stores", auth=self.auth).json()
        n = len(stores) if isinstance(stores, list) else 0
        return f"ShipStation ({n} store{'s' if n != 1 else ''})"

    def fetch(self, since: datetime) -> list[StoreOrder]:
        out: list[StoreOrder] = []
        with client() as c:
            for page in range(1, MAX_PAGES + 1):
                body = _get(
                    c,
                    self.who,
                    f"{SHIPSTATION_BASE}/orders",
                    auth=self.auth,
                    params={
                        "orderStatus": "awaiting_shipment",
                        "createDateStart": since.strftime("%Y-%m-%d %H:%M:%S"),
                        "pageSize": 100,
                        "page": page,
                        "sortBy": "OrderDate",
                        "sortDir": "ASC",
                    },
                ).json()
                for o in body.get("orders") or []:
                    ship_to = o.get("shipTo") or {}
                    so = StoreOrder(
                        store_order_id=str(o["orderId"]),
                        number=str(o.get("orderNumber") or o["orderId"]),
                        customer=ship_to.get("company") or ship_to.get("name"),
                        lines=[],
                        rush=bool(EXPEDITED.search(str(o.get("requestedShippingService") or ""))),
                        ship_by=order_svc.parse_date(o.get("shipByDate")),
                    )
                    for it in o.get("items") or []:
                        if it.get("adjustment"):
                            continue
                        line = _line(it.get("upc"), it.get("sku"), int(it.get("quantity") or 0), it.get("name"))
                        if line:
                            so.lines.append(line)
                        else:
                            so.skipped_lines.append(str(it.get("name") or "item"))
                    out.append(so)
                if page >= int(body.get("pages") or 1):
                    break
        return out

    def products(self) -> list[StoreProduct]:
        out: list[StoreProduct] = []
        with client() as c:
            for page in range(1, 21):
                body = _get(
                    c, self.who, f"{SHIPSTATION_BASE}/products", auth=self.auth, params={"pageSize": 500, "page": page}
                ).json()
                for p in body.get("products") or []:
                    oz = p.get("weightOz")
                    out.append(
                        StoreProduct(
                            external_id=str(p.get("productId")),
                            name=(p.get("name") or p.get("sku") or "Product")[:500],
                            sku=(p.get("sku") or "").strip()[:100] or None,
                            barcode=(p.get("upc") or "").strip()[:200] or None,
                            image_url=p.get("thumbnailUrl") or None,
                            weight_grams=round(float(oz) * 28.3495) if oz else None,
                            location=(p.get("warehouseLocation") or "").strip()[:100] or None,
                        )
                    )
                if page >= int(body.get("pages") or 1):
                    break
        return out

    def push(self, order: Order) -> str:
        with client() as c:
            _send(
                c,
                self.who,
                "POST",
                f"{SHIPSTATION_BASE}/orders/markasshipped",
                auth=self.auth,
                json={
                    "orderId": int(order.store_order_id or 0),
                    "carrierCode": SHIPSTATION_CARRIERS.get(order.carrier or "", "other"),
                    "shipDate": (order.shipped_at or utcnow()).strftime("%Y-%m-%d"),
                    "trackingNumber": order.tracking_number,
                    "notifyCustomer": True,
                    "notifySalesChannel": True,
                },
            )
        return "marked shipped in ShipStation; customer and sales channel notified"


# ---------------------------------------------------------------------------
# WooCommerce (REST API v3, consumer key + secret)
# ---------------------------------------------------------------------------


def woo_url(raw: str) -> str:
    s = (raw or "").strip().rstrip("/")
    if s and not s.startswith(("http://", "https://")):
        s = "https://" + s
    s = re.sub(r"/wp-admin.*$", "", s)
    return check_public_url(s)


class WooCommerce:
    who = "WooCommerce"

    def __init__(self, config: dict[str, Any], secret: dict[str, Any]) -> None:
        self.base = woo_url(config.get("store_url", "")) + "/wp-json/wc/v3"
        self.auth = (str(secret.get("consumer_key") or ""), str(secret.get("consumer_secret") or ""))
        if not all(self.auth):
            raise StoreError(
                "Paste the consumer key and secret (WooCommerce → Settings → Advanced → REST API, "
                "permissions Read/Write).",
                retry=False,
            )

    def _get(self, c: httpx.Client, path: str, **params: Any) -> Any:
        resp = _get(c, self.who, f"{self.base}{path}", auth=self.auth, params=params)
        try:
            return resp.json()
        except ValueError as exc:
            raise StoreError(
                "That site didn't answer like a WooCommerce store. Check the address and that permalinks are on."
            ) from exc

    def check(self) -> str:
        with client() as c:
            self._get(c, "/orders", per_page=1)
        return urlparse(self.base).hostname or "WooCommerce"

    def _barcodes(self, c: httpx.Client, items: list[dict[str, Any]]) -> dict[int, str]:
        """GTIN/UPC/EAN per product or variation id (WooCommerce 9.2+
        `global_unique_id`). Older stores fall back to the SKU."""
        found: dict[int, str] = {}
        products = sorted({int(i["product_id"]) for i in items if i.get("product_id")})
        for k in range(0, len(products), 100):
            chunk = products[k : k + 100]
            for p in self._get(c, "/products", include=",".join(map(str, chunk)), per_page=100):
                if p.get("global_unique_id"):
                    found[int(p["id"])] = str(p["global_unique_id"])
        by_parent: dict[int, set[int]] = {}
        for i in items:
            if i.get("variation_id"):
                by_parent.setdefault(int(i["product_id"]), set()).add(int(i["variation_id"]))
        for parent, vids in list(by_parent.items())[:50]:
            ids = ",".join(map(str, sorted(vids)))
            for v in self._get(c, f"/products/{parent}/variations", include=ids, per_page=100):
                if v.get("global_unique_id"):
                    found[int(v["id"])] = str(v["global_unique_id"])
        return found

    def fetch(self, since: datetime) -> list[StoreOrder]:
        raw: list[dict[str, Any]] = []
        with client() as c:
            for page in range(1, MAX_PAGES + 1):
                batch = self._get(
                    c,
                    "/orders",
                    status="processing",
                    after=since.strftime("%Y-%m-%dT%H:%M:%S"),
                    per_page=100,
                    page=page,
                    order="asc",
                )
                raw.extend(batch)
                if len(batch) < 100:
                    break
            items = [li for o in raw for li in o.get("line_items") or []]
            codes = self._barcodes(c, items) if items else {}
        out: list[StoreOrder] = []
        for o in raw:
            ship = o.get("shipping") or {}
            bill = o.get("billing") or {}
            person = " ".join(x for x in (ship.get("first_name"), ship.get("last_name")) if x)
            so = StoreOrder(
                store_order_id=str(o["id"]),
                number=str(o.get("number") or o["id"]),
                customer=ship.get("company") or person or bill.get("company") or None,
                lines=[],
            )
            for li in o.get("line_items") or []:
                pid = int(li.get("variation_id") or li.get("product_id") or 0)
                line = _line(codes.get(pid), li.get("sku"), int(li.get("quantity") or 0), li.get("name"))
                if line:
                    so.lines.append(line)
                else:
                    so.skipped_lines.append(str(li.get("name") or "item"))
            out.append(so)
        return out

    def products(self) -> list[StoreProduct]:
        out: list[StoreProduct] = []

        def grams(v: Any) -> int | None:
            try:
                return round(float(v) * self.weight_factor) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        with client() as c:
            variable: list[dict[str, Any]] = []
            for page in range(1, 51):
                batch = self._get(c, "/products", per_page=100, page=page, status="publish")
                for p in batch:
                    image = ((p.get("images") or [{}])[0] or {}).get("src")
                    if p.get("type") == "variable":
                        variable.append({**p, "_image": image})
                        continue
                    out.append(
                        StoreProduct(
                            external_id=str(p["id"]),
                            name=(p.get("name") or "Product")[:500],
                            sku=(p.get("sku") or "").strip()[:100] or None,
                            barcode=(p.get("global_unique_id") or "").strip()[:200] or None,
                            image_url=image,
                            weight_grams=grams(p.get("weight")),
                        )
                    )
                if len(batch) < 100:
                    break
            for p in variable[:100]:
                for v in self._get(c, f"/products/{p['id']}/variations", per_page=100):
                    attrs = ", ".join(str(a.get("option")) for a in v.get("attributes") or [] if a.get("option"))
                    out.append(
                        StoreProduct(
                            external_id=f"{p['id']}:{v['id']}",
                            name=f"{p.get('name')}{' - ' + attrs if attrs else ''}"[:500],
                            sku=(v.get("sku") or "").strip()[:100] or None,
                            barcode=(v.get("global_unique_id") or "").strip()[:200] or None,
                            image_url=(v.get("image") or {}).get("src") or p.get("_image"),
                            weight_grams=grams(v.get("weight")),
                        )
                    )
        return out

    # WooCommerce reports weight in the store's unit; kg is the default.
    weight_factor = 1000.0

    def push(self, order: Order) -> str:
        carrier = f" via {order.carrier}" if order.carrier else ""
        with client() as c:
            _send(
                c,
                self.who,
                "POST",
                f"{self.base}/orders/{order.store_order_id}/notes",
                auth=self.auth,
                json={"note": f"Shipped{carrier}. Tracking number: {order.tracking_number}", "customer_note": True},
            )
            _send(
                c,
                self.who,
                "PUT",
                f"{self.base}/orders/{order.store_order_id}",
                auth=self.auth,
                json={"status": "completed"},
            )
        return "completed in WooCommerce; tracking sent to the customer as an order note"


# ---------------------------------------------------------------------------
# Spreadsheet link (Google Sheets or any CSV URL)
# ---------------------------------------------------------------------------


def sheet_csv_url(raw: str) -> str:
    """Turn a Google Sheets link (edit, view or share) into its CSV export."""
    url = (raw or "").strip()
    m = re.match(r"https://docs\.google\.com/spreadsheets/d/([\w-]+)", url)
    if m and "/export" not in url and "output=csv" not in url and "/pub" not in url:
        gid = re.search(r"[#&?]gid=(\d+)", url)
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv" + (
            f"&gid={gid.group(1)}" if gid else ""
        )
    return url


class SheetLink:
    who = "The spreadsheet link"

    def __init__(self, config: dict[str, Any], secret: dict[str, Any]) -> None:
        self.url = check_public_url(sheet_csv_url(config.get("url", "")))

    def download(self) -> bytes:
        resp = fetch_public(self.url, self.who)
        ctype = resp.headers.get("content-type", "")
        if "text/html" in ctype:
            raise StoreError(
                "The link opened a web page, not a CSV. In Google Sheets use Share → "
                "'Anyone with the link can view', or File → Share → Publish to web → CSV.",
                retry=False,
            )
        return resp.content

    def check(self) -> str:
        from . import csv_import

        pr = csv_import.parse(self.download())
        return f"Spreadsheet ({len(pr.orders)} order{'s' if len(pr.orders) != 1 else ''} in it now)"

    def fetch(self, since: datetime) -> list[StoreOrder]:
        raise NotImplementedError  # sheets go through the CSV importer

    def push(self, order: Order) -> str:
        return "spreadsheets don't take tracking back"

    def products(self) -> list[StoreProduct]:
        return []


Connector = Shopify | ShipStation | WooCommerce | SheetLink
CONNECTORS: dict[IntegrationKind, Callable[[dict[str, Any], dict[str, Any]], Connector]] = {
    IntegrationKind.shopify: Shopify,
    IntegrationKind.shipstation: ShipStation,
    IntegrationKind.woocommerce: WooCommerce,
    IntegrationKind.sheet: SheetLink,
}


def connector(kind: IntegrationKind, config: dict[str, Any], secret: dict[str, Any]) -> Connector:
    return CONNECTORS[kind](config, secret)


def since(now: datetime) -> datetime:
    """How far back to look. A generous window: orders already pulled are
    skipped, and a store that was unreachable for a day still gets its
    backlog."""
    return now - timedelta(days=LOOKBACK_DAYS)
