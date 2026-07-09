FROM python:3.11.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY src ./src
COPY sandbox/entrypoint.sh /usr/local/bin/tomo-sandbox-entrypoint

RUN pip install --no-cache-dir "uv==0.6.14" \
    && uv sync --frozen --no-dev \
    && chmod 755 /usr/local/bin/tomo-sandbox-entrypoint

ENTRYPOINT ["/usr/local/bin/tomo-sandbox-entrypoint"]
