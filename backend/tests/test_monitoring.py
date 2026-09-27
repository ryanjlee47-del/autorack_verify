"""Error recording and operator alerts."""

from __future__ import annotations

import pytest
from conftest import last_link_token, signup
from sqlalchemy import select

from autorack.config import get_settings
from autorack.models import ErrorEvent, utcnow
from autorack.services import email, jobs, monitoring


@pytest.fixture
def ops(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "operator_emails", "ops@example.com")
    client.post("/api/auth/magic-link", json={"email": "ops@example.com"})
    token = client.post("/api/auth/verify", json={"token": last_link_token("ops@example.com")}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def boom():
    raise ValueError("kaboom")


def test_server_errors_are_recorded_grouped_and_alerted_once(app, ops, db, monkeypatch):
    from fastapi.testclient import TestClient

    from autorack.api import reporting

    monkeypatch.setattr(reporting.dash, "summary", lambda *a, **k: boom())
    owner_client = TestClient(app, raise_server_exceptions=False)
    owner = signup(owner_client)
    for _ in range(3):
        r = owner_client.get("/api/dashboard/summary", headers=owner.h)
        assert r.status_code == 500
        assert r.json()["detail"]["code"] == "server_error" and "kaboom" not in r.text
    events = list(db.scalars(select(ErrorEvent)))
    assert len(events) == 1 and events[0].count == 3 and events[0].kind == "ValueError"
    assert events[0].context["path"] == "/api/dashboard/summary"
    assert "Traceback" in events[0].detail

    email.outbox.clear()
    db.expire_all()
    jobs.run_all(db)
    alerts = [m for m in email.outbox if m.to == "ops@example.com"]
    assert len(alerts) == 1 and "1 error" in alerts[0].subject and "kaboom" in alerts[0].text
    jobs.run_all(db)
    assert len([m for m in email.outbox if m.to == "ops@example.com"]) == 1  # not again

    # Resolved, then it happens again: alerted straight away.
    listed = owner_client.get("/api/admin/errors", headers=ops).json()
    assert listed[0]["count"] == 3
    owner_client.post(f"/api/admin/errors/{listed[0]['id']}/resolve", headers=ops)
    assert owner_client.get("/api/admin/errors", headers=ops).json() == []
    owner_client.get("/api/dashboard/summary", headers=owner.h)
    db.expire_all()
    jobs.run_all(db)
    assert len([m for m in email.outbox if m.to == "ops@example.com"]) == 2
    detail = owner_client.get(f"/api/admin/errors/{listed[0]['id']}", headers=ops).json()
    assert detail["count"] == 4 and "ValueError" in detail["detail"]


def test_browser_errors_recorded_and_noise_ignored(client, db):
    r = client.post(
        "/api/client-errors",
        json={
            "app": "phone",
            "message": "TypeError: x is undefined",
            "stack": "at render (https://h/w/js/app.js:120:5)",
            "page": "/w/",
        },
    )
    assert r.status_code == 202 and r.json()["recorded"] is True
    client.post(
        "/api/client-errors",
        json={
            "app": "phone",
            "message": "TypeError: x is undefined",
            "stack": "at render (https://other/w/js/app.js:120:5)",
        },
    )
    assert client.post("/api/client-errors", json={"message": "ResizeObserver loop limit exceeded"}).json() == {
        "recorded": False
    }
    events = list(db.scalars(select(ErrorEvent)))
    assert len(events) == 1 and events[0].count == 2 and events[0].source == "browser"


def test_failed_job_is_recorded(db, monkeypatch):
    monkeypatch.setattr(jobs, "run_daily_summaries", lambda db, now: boom())
    out = jobs.run_all(db)
    assert out["daily_summaries"] == "error"
    e = db.scalar(select(ErrorEvent))
    assert e.source == "job" and e.context == {"job": "daily_summaries"}


def test_health_reports_job_runs(client, db):
    jobs.run_all(db)
    body = client.get("/api/health").json()
    assert body["ok"] is True and body["jobs_last_run"]


def test_recording_never_raises(monkeypatch):
    monkeypatch.setattr("autorack.db.get_sessionmaker", lambda: (_ for _ in ()).throw(RuntimeError("db gone")))
    monitoring.record_exception(ValueError("x"))  # logs, doesn't raise
    assert utcnow()
