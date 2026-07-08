# Trilium ETAPI MCP server

A standalone [MCP](https://modelcontextprotocol.io) server that exposes the
[Trilium](https://triliumnotes.org) [ETAPI](https://github.com/TriliumNext/Trilium)
(External API) as MCP tools. It runs as a **container sidecar** next to your Trilium
instance: every documented ETAPI endpoint is turned into an MCP tool at startup via
`FastMCP.from_openapi` (currently **40 tools** — `createNote`, `getNoteById`,
`searchNotes`, `exportNoteSubtree`, …), served over streamable **HTTP** so any MCP
client connects to it by URL.

## Architecture

```
MCP client (Claude Code, …)  ──HTTP /mcp──▶  mcp sidecar  ──ETAPI──▶  trilium
                                             (this repo)   http://trilium:8080/etapi
```

The sidecar talks to Trilium over the internal Docker network, so Trilium's ETAPI is
never exposed publicly on its own.

## Setup

1. **Create an ETAPI token** in Trilium: *Options → ETAPI → Create new ETAPI token*.
2. **Configure** — copy `.env.example` to `.env` and fill in:
   - `TRILIUM_ETAPI_TOKEN` (required)
   - `MCP_AUTH_TOKEN` (optional bearer token protecting the MCP endpoint — see Security)
3. **Run** both Trilium and the sidecar:
   ```
   docker compose up -d --build
   ```
   The MCP endpoint is then available at `http://localhost:8000/mcp`.

## Connecting a client

The MCP endpoint speaks streamable HTTP. For Claude Code:

```
claude mcp add --transport http trilium http://localhost:8000/mcp \
  --header "Authorization: Bearer YOUR_MCP_AUTH_TOKEN"
```

Or use the provided [`.mcp.json`](.mcp.json) (drop the `headers` block if you did not set
`MCP_AUTH_TOKEN`).

## Configuration

All configuration is via environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `TRILIUM_ETAPI_TOKEN` | *(required)* | Trilium ETAPI token, sent in the `Authorization` header to Trilium. |
| `TRILIUM_SERVER_URL` | `http://trilium:8080` | Base URL of the Trilium instance (`/etapi` is appended automatically). |
| `MCP_AUTH_TOKEN` | *(unset)* | If set, clients must send `Authorization: Bearer <token>`. |
| `MCP_HOST` | `0.0.0.0` | Interface the MCP server binds to. |
| `MCP_PORT` | `8000` | Port the MCP server listens on. |
| `MCP_PATH` | `/mcp` | HTTP path the MCP endpoint is served at. |
| `TRILIUM_ETAPI_SPEC` | bundled spec | Override the OpenAPI spec path. |

## Security

The MCP endpoint grants **full read/write access to your notes**. When it is reachable
beyond localhost (e.g. behind a reverse proxy), set `MCP_AUTH_TOKEN` so clients must
present a bearer token, and/or restrict access at the network layer. Without the token
the endpoint is open. The `/health` endpoint is always unauthenticated (used by the
container healthcheck).

If `TRILIUM_ETAPI_TOKEN` is missing or the spec cannot be loaded, the server still
starts and completes the MCP handshake, but exposes only a single `startup_error` tool
describing how to fix it (rather than failing with an opaque connection error).

## Layout

```
docker-compose.yaml      trilium + mcp sidecar
Dockerfile               builds the MCP server image (uv-based)
.env.example             configuration template
.mcp.json                example MCP client config
app/
  server.py              the MCP server (OpenAPI-driven, HTTP transport, bearer auth)
  pyproject.toml         dependencies
  uv.lock
  trillium-etapi.openapi bundled Trilium ETAPI OpenAPI spec
```
