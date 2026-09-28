"""Safeguards added in the security review. Each test names the attack it
stops, so a regression reads as the hole it reopens."""

from __future__ import annotations

import http.server
import io
import threading
from datetime import timedelta
from typing import Any

import pytest
from conftest import add_worker, link_phone, signup
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image
from reportlab.platypus import SimpleDocTemplate
from sqlalchemy import update
from starlette.datastructures import Headers

from autorack import deps
from autorack.config import Settings, get_settings
from autorack.downloads import attachment, csv_text, safe_cell
from autorack.errors import ApiError
from autorack.main import BodySizeLimit
from autorack.models import Device, utcnow
from autorack.services import catalog, email, jobs, monthly, stores

# ---------------------------------------------------------------------------
# Client addresses: X-Forwarded-For is client-controlled on the left
# ---------------------------------------------------------------------------


def _request(xff: str | None, peer: str = "10.0.0.5") -> Request:
    headers = Headers({"x-forwarded-for": xff} if xff else {})
    return Request({"type": "http", "headers": headers.raw, "client": (peer, 1234)})


def test_client_ip_uses_the_entry_our_proxy_added(monkeypatch):
    assert deps.client_ip(_request("6.6.6.6, 203.0.113.9")) == "203.0.113.9"  # 6.6.6.6 was typed by the client
    assert deps.client_ip(_request(None)) == "10.0.0.5"
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 2)  # e.g. Cloudflare in front of Render
    assert deps.client_ip(_request("6.6.6.6, 203.0.113.9, 172.68.1.1")) == "203.0.113.9"
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 0)
    assert deps.client_ip(_request("6.6.6.6")) == "10.0.0.5"


def test_spoofed_forwarded_for_does_not_reset_rate_limits(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 1)
    codes = []
    for i in range(12):
        r = client.post(
            "/api/worker/link",
            json={"join_code": "ZZZZ-ZZZZ", "label": "x"},
            headers={"X-Forwarded-For": f"198.51.100.{i}, 203.0.113.50"},
        )
        codes.append(r.status_code)
    assert codes[-1] == 429  # same real address every time, whatever the client claimed


# ---------------------------------------------------------------------------
# Outgoing requests: private addresses are refused at connect time
# ---------------------------------------------------------------------------


@pytest.fixture
def internal_server() -> Any:
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"internal secret")

        def log_message(self, *a: Any) -> None:
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], hits
    srv.shutdown()


def test_dns_rebinding_cannot_reach_internal_addresses(internal_server, monkeypatch):
    """The name passed the up-front check, then resolves somewhere private
    when the connection is made: refused, and nothing is fetched."""
    port, hits = internal_server
    monkeypatch.setattr(stores, "TRANSPORT", None)
    with stores.client() as c:
        for url in (f"http://127.0.0.1:{port}/", f"http://localhost:{port}/", f"http://[::ffff:127.0.0.1]:{port}/"):
            with pytest.raises(Exception, match="private"):
                c.get(url)
    assert hits == []


def test_public_ip_rules():
    assert stores.is_public_ip("8.8.8.8")
    for private in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "100.64.0.1", "::1", "::ffff:192.168.0.1", "fd00::1"):
        assert not stores.is_public_ip(private), private


# ---------------------------------------------------------------------------
# Worker PINs: guessing spread over a day, from freshly linked phones
# ---------------------------------------------------------------------------


def _wrong(client, phone, n: int, pin: str = "9876") -> list[int]:
    return [client.post("/api/worker/login", json={"pin": pin}, headers=phone.h).status_code for _ in range(n)]


def test_phone_has_a_daily_allowance_of_wrong_pins(client, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "pin_max_failures_per_device", 1000)  # isolate the daily rule
    monkeypatch.setattr(get_settings(), "pin_max_failures_per_warehouse", 1000)
    owner = signup(client)
    add_worker(client, owner, pin="2468")
    phone = link_phone(client, owner)
    assert set(_wrong(client, phone, 15)) == {401}
    assert _wrong(client, phone, 1) == [429]
    # A correct PIN doesn't buy more guesses today.
    assert client.post("/api/worker/login", json={"pin": "2468"}, headers=phone.h).status_code == 429


def test_new_phones_are_shut_out_after_a_warehouse_wide_daily_cap(client, db, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "pin_max_failures_per_warehouse", 1000)
    monkeypatch.setattr(s, "pin_max_failures_per_warehouse_day", 20)
    owner = signup(client)
    add_worker(client, owner, pin="2468")
    old_phone = link_phone(client, owner, "Dock phone")
    db.execute(update(Device).values(created_at=utcnow() - timedelta(days=3)))
    db.commit()
    attackers = [link_phone(client, owner, f"x{i}") for i in range(5)]
    for p in attackers:
        _wrong(client, p, 4)  # 4 each: under the per-phone lockout
    fresh = link_phone(client, owner, "x-new")
    r = client.post("/api/worker/login", json={"pin": "2468"}, headers=fresh.h)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "pin_locked"
    # The warehouse's own phone still works.
    assert client.post("/api/worker/login", json={"pin": "2468"}, headers=old_phone.h).status_code == 200


def test_setup_code_links_a_limited_number_of_phones_per_hour(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_device_links_per_hour", 3)
    owner = signup(client)
    code = client.get("/api/warehouse/device-link", headers=owner.h).json()["join_code"]
    got = [
        client.post(
            "/api/worker/link",
            json={"join_code": code, "label": "p"},
            headers={"X-Forwarded-For": f"203.0.113.{i}"},  # even from different addresses
        ).status_code
        for i in range(4)
    ]
    assert got == [200, 200, 200, 429]


def test_owners_are_told_about_pin_guessing_once_a_day(client, db, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "pin_alert_failures_day", 6)
    monkeypatch.setattr(s, "pin_max_failures_per_warehouse", 1000)
    owner = signup(client, name="Dockside")
    add_worker(client, owner, pin="2468")
    for i in range(2):
        _wrong(client, link_phone(client, owner, f"p{i}"), 3)
    email.outbox.clear()
    assert jobs.run_pin_guess_alerts(db, utcnow()) == 1
    msg = email.outbox[-1]
    assert msg.to == owner.email and "6 wrong PINs" in msg.subject and "change the setup code" in msg.text
    assert jobs.run_pin_guess_alerts(db, utcnow()) == 0


# ---------------------------------------------------------------------------
# PDFs: names are text, not markup
# ---------------------------------------------------------------------------


def test_monthly_report_tables_treat_names_as_text(tmp_path):
    img = tmp_path / "secret.png"
    Image.new("RGB", (4, 4), "red").save(img)
    evil = [[f'Widget <img src="{img}" width="40" height="40"/>', "<b>unclosed", "Nuts & Bolts <3"]]
    buf = io.BytesIO()
    SimpleDocTemplate(buf).build([monthly._table(["Item", "A", "B"], evil, [200, 100, 100])])
    pdf = buf.getvalue()
    assert pdf.startswith(b"%PDF") and b"/Subtype /Image" not in pdf  # the file was not pulled in


# ---------------------------------------------------------------------------
# CSV exports and download names
# ---------------------------------------------------------------------------


def test_csv_cells_never_become_formulas():
    assert safe_cell('=HYPERLINK("http://evil","x")').startswith("'")
    assert safe_cell("+cmd|' /C calc'!A0").startswith("'")
    assert safe_cell("@SUM(A1)").startswith("'")
    assert safe_cell("-5") == "-5" and safe_cell(12) == "12" and safe_cell(None) == ""
    text = csv_text(["barcode"], [["=1+1"], ["012345678905"]])
    assert "'=1+1" in text and "012345678905" in text


def test_count_variance_export_neutralizes_scanned_barcodes(client, db):
    from conftest import scan, worker_on_phone
    from test_tasks import create_task

    owner = signup(client)
    phone = worker_on_phone(client, owner)
    order = create_task(client, owner, "count", [("012345678905", 2)])
    scan(client, phone, order["id"], '=HYPERLINK("http://evil.example","open")')
    r = client.get(f"/api/orders/{order['id']}/variance.csv", headers=owner.h)
    assert r.status_code == 200
    assert "'=HYPERLINK" in r.text


def test_download_names_in_any_script():
    h = attachment("autorack-evidence-訂單1001.pdf")
    h.encode("latin-1")  # a valid header value
    assert 'filename="autorack-evidence-1001.pdf"' in h and "filename*=UTF-8''" in h
    assert attachment("report.pdf", "inline") == 'inline; filename="report.pdf"'
    assert 'filename="a-b.csv"' in attachment('a"\r\nb.csv')


# ---------------------------------------------------------------------------
# Request size, headers, config
# ---------------------------------------------------------------------------


def test_oversized_bodies_are_refused_before_they_are_read():
    app = FastAPI()

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"n": len(await request.body())}

    app.add_middleware(BodySizeLimit, limit=1000)
    c = TestClient(app)
    assert c.post("/echo", content=b"x" * 999).json() == {"n": 999}
    assert c.post("/echo", content=b"x" * 5000).status_code == 413

    def chunks() -> Any:  # no Content-Length: counted as it arrives
        for _ in range(10):
            yield b"x" * 500

    assert c.post("/echo", content=chunks()).status_code == 413


def test_request_id_is_not_reflected_raw(client):
    assert client.get("/api/health", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"] == "abc-123"
    rid = client.get("/api/health", headers={"X-Request-ID": "<script>alert(1)</script>"}).headers["x-request-id"]
    assert "<" not in rid


def test_production_refuses_open_cors_and_weak_cron_secret():
    s = Settings(
        environment="production",
        secret_key="x" * 40,
        google_client_id="id",
        google_client_secret="secret",
        email_backend="resend",
        resend_api_key="re_x",
        frontend_url="https://app.example.com",
        cors_origins="*,http://evil.example",
        cron_secret="short",
    )
    problems = " ".join(s.validate_for_production())
    assert "CORS_ORIGINS" in problems and "CRON_SECRET" in problems


def test_images_only_in_the_formats_we_promise():
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "TIFF")
    with pytest.raises(ApiError) as err:
        catalog.process_image(buf.getvalue())
    assert err.value.code == "image_invalid"
    ok = io.BytesIO()
    Image.new("RGB", (8, 8)).save(ok, "PNG")
    full, _thumb = catalog.process_image(ok.getvalue())
    assert full[:3] == b"\xff\xd8\xff"


def test_email_subjects_cannot_carry_extra_headers():
    msg = email.Email(to="a@example.com", subject="Dockside\r\nBcc: victim@example.com", text="t", html="h")
    email.send(msg)
    assert "\n" not in email.outbox[-1].subject and "\r" not in email.outbox[-1].subject
