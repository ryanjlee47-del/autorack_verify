# Autorack API (FastAPI). Build from the repository root:
#   docker build -t autorack .
# The image also carries the static frontend, so one container can serve
# everything (SERVE_FRONTEND=true) when not using Cloudflare Pages.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    ENVIRONMENT=production SERVE_FRONTEND=false FRONTEND_DIR=/app/frontend

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt
COPY backend backend
COPY frontend frontend

RUN useradd --create-home --uid 10001 autorack
USER autorack
WORKDIR /app/backend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/api/health', timeout=4)"
CMD ["./start.sh"]
