FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY contract ./contract
RUN useradd --create-home appuser && mkdir -p /data && chown appuser /data
USER appuser
ENV DATABASE_PATH=/data/dashboard.db PORT=8001
EXPOSE 8001
# One worker: idempotency locks and the SSE broadcaster are in-process.
CMD ["sh", "-c", "uv run --no-dev uvicorn --factory app.main:create_app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
