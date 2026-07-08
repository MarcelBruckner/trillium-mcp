# Standalone Trilium ETAPI MCP server, run as a sidecar next to Trilium.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Install dependencies first (cached unless the lock changes).
COPY app/pyproject.toml app/uv.lock ./
RUN uv sync --frozen --no-dev

# Then the application code + bundled OpenAPI spec.
COPY app/server.py app/trillium-etapi.openapi ./

EXPOSE 8000

# All configuration is via environment variables (see README / docker-compose).
CMD ["uv", "run", "--frozen", "--no-dev", "python", "server.py"]
