# Application image: API, workers, CLI. Runs as a non-root user (NFR-S-08).
FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY clickhouse ./clickhouse
COPY reference ./reference
COPY rules ./rules
RUN uv sync --frozen --no-dev

RUN groupadd --system payintel && useradd --system --gid payintel --home /app payintel \
    && chown -R payintel:payintel /app
USER payintel

ENV PATH="/opt/venv/bin:${PATH}"
ENTRYPOINT []
CMD ["payintel", "--help"]
