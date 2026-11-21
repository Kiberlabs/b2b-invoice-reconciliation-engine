# Multi-stage production Dockerfile for the KiberLabs Invoice Reconciliation Engine
FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11-slim AS runtime

RUN groupadd --system appuser && useradd --system --gid appuser --create-home appuser
WORKDIR /app

COPY --from=builder /install /usr/local

RUN mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser

# INVOICE_ENGINE_API_KEY must be supplied at `docker run -e ...`; the app refuses to start without it.
ENV INVOICE_ENGINE_DB=/app/data/ledger.db \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" || exit 1

CMD ["uvicorn", "invoice_engine.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
