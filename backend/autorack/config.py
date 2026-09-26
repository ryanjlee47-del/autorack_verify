"""Runtime configuration, read from environment variables (or a .env file).

Every setting has a development-friendly default so `uvicorn autorack.main:app`
works on a laptop with a local Postgres. `validate_for_production()` refuses to
boot a production process that still carries a development default for
anything security-relevant.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_SECRET = "dev-only-secret-change-me-dev-only-secret-change-me"  # noqa: S105


class Settings(BaseSettings):
    # backend/.env, wherever the process was started from (some hosts, e.g.
    # PythonAnywhere, don't start it in backend/). A .env in the working
    # directory, if different, is read too and wins.
    model_config = SettingsConfigDict(env_file=(REPO_ROOT / "backend" / ".env", ".env"), extra="ignore")

    environment: Literal["development", "test", "production"] = "development"

    # Neon hands out URLs like postgresql://user:pw@host/db?sslmode=require.
    # They are rewritten to the psycopg 3 driver in normalize_db_url().
    database_url: str = "postgresql+psycopg://postgres@localhost:5432/autorack_dev"
    db_pool_size: int = 5
    db_max_overflow: int = 5

    # Keys the PIN fingerprint HMAC. Rotating it invalidates every worker PIN
    # lookup, so treat it like a database credential, not a session secret.
    secret_key: str = DEV_SECRET

    # Where people land from emails and QR codes (the Cloudflare Pages site).
    frontend_url: str = "http://localhost:8000"
    # Origins allowed to call the API from a browser. Comma-separated.
    cors_origins: str = "http://localhost:8000,http://127.0.0.1:8000"
    # Serve ../frontend from this process. Handy locally and for single-host
    # deploys; turn off when the frontend lives on Cloudflare Pages.
    serve_frontend: bool = True
    frontend_dir: Path = REPO_ROOT / "frontend"

    # Owner auth
    magic_link_ttl_minutes: int = 15
    owner_session_days: int = 30
    signup_enabled: bool = True

    # Worker auth
    worker_session_hours: int = 14
    pin_length: int = 4
    pin_max_failures_per_device: int = 5
    pin_max_failures_per_warehouse: int = 25
    pin_lockout_minutes: int = 10

    # Email
    email_backend: Literal["console", "memory", "smtp", "resend"] = "console"
    email_from: str = "Autorack <login@autorack.local>"
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    resend_api_key: str = ""

    # Billing: flat monthly price per warehouse
    plan_price_cents: int = 17500
    plan_name: str = "Autorack — per warehouse"
    trial_days: int = 14
    past_due_grace_days: int = 7
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id: str = ""

    # Limits
    max_import_bytes: int = 5 * 1024 * 1024
    max_import_rows: int = 50_000
    max_sync_events: int = 500
    sync_requests_per_minute: int = 240
    import_requests_per_minute: int = 12

    # Photos workers attach to problems (JPEG/WebP, compressed on the phone)
    max_photo_bytes: int = 1_500_000

    # Operator (you, running Autorack): these emails can open /admin/ and see
    # every warehouse. Comma-separated. They sign in like anyone else.
    operator_emails: str = ""

    # Background jobs (daily summary, alerts, trial and payment emails).
    # Run in-process every minute; free hosts that sleep when idle should also
    # call POST /api/cron/run with this secret from an external cron.
    jobs_enabled: bool = True
    cron_secret: str = ""

    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def normalize_db_url(cls, v: str) -> str:
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix) :]
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()]

    @property
    def operator_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.operator_emails.split(",") if e.strip()}

    @property
    def stripe_enabled(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_price_id)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def validate_for_production(self) -> list[str]:
        """Return a list of problems that make this config unsafe to serve."""
        problems: list[str] = []
        if not self.is_production:
            return problems
        if self.secret_key == DEV_SECRET or len(self.secret_key) < 32:
            problems.append("SECRET_KEY must be set to a random value of at least 32 characters.")
        if self.email_backend in ("console", "memory"):
            problems.append("EMAIL_BACKEND must be smtp or resend; owners cannot log in without email.")
        if self.email_backend == "resend" and not self.resend_api_key:
            problems.append("RESEND_API_KEY is required when EMAIL_BACKEND=resend.")
        if not self.frontend_url.startswith("https://"):
            problems.append("FRONTEND_URL must be https:// (magic links and camera access need TLS).")
        if self.stripe_secret_key and not self.stripe_webhook_secret:
            problems.append("STRIPE_WEBHOOK_SECRET is required when STRIPE_SECRET_KEY is set.")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
