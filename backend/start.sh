#!/bin/sh
# Production entrypoint: apply migrations, then serve.
# Migrations are transactional (Postgres DDL), so a failed one leaves the old
# schema in place and the deploy fails loudly instead of serving half a schema.
set -eu
cd "$(dirname "$0")"
python -m autorack.cli check-config
alembic upgrade head
exec uvicorn autorack.main:app \
  --host 0.0.0.0 --port "${PORT:-8000}" \
  --proxy-headers --forwarded-allow-ips='*' \
  --workers "${WEB_CONCURRENCY:-2}"
