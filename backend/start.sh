#!/bin/sh
# Production entrypoint: apply migrations, then serve.
# Migrations are transactional (Postgres DDL), so a failed one leaves the old
# schema in place and the deploy fails loudly instead of serving half a schema.
# Proxy headers are read by the app itself (deps.client_ip, TRUSTED_PROXY_HOPS):
# uvicorn's --forwarded-allow-ips='*' would trust the client-supplied entry.
set -eu
cd "$(dirname "$0")"
python -m autorack.cli check-config
alembic upgrade head
exec uvicorn autorack.main:app \
  --host 0.0.0.0 --port "${PORT:-8000}" \
  --no-proxy-headers \
  --workers "${WEB_CONCURRENCY:-2}"
