# Trilium ETAPI MCP server

A standalone [MCP](https://modelcontextprotocol.io) server that exposes the
[Trilium](https://triliumnotes.org) [ETAPI](https://github.com/TriliumNext/Trilium)
(External API) as MCP tools. It runs as a **container sidecar** next to your Trilium
instance: every documented ETAPI endpoint is turned into an MCP tool at startup via
`FastMCP.from_openapi` (currently **40 tools** — `createNote`, `getNoteById`,
`searchNotes`, `exportNoteSubtree`, …), served over streamable **HTTP** so any MCP
client connects to it by URL.

## Architecture

<p align="center">
  <img src="docs/architecture.png" alt="Deployment: MCP clients → mcp sidecar → Trilium, on the Docker network" width="520">
</p>

The sidecar talks to Trilium over the internal Docker network, so Trilium's ETAPI is
never exposed publicly on its own. Clients reach the sidecar either through a TLS-terminating
reverse proxy or directly over a trusted LAN — in both cases the ETAPI token they present is
the only credential.

## How a request flows

At a glance, the sidecar forwards the client's ETAPI token straight through to Trilium:

<p align="center">
  <img src="docs/sequence-overview.png" alt="Client sends a tool call with an ETAPI token; the sidecar forwards it to Trilium and returns the result" width="560">
</p>

In more detail — startup builds the tools from the OpenAPI spec, the middleware rejects any
request without an `Authorization` header, and the token is carried per request from the
middleware to the outgoing ETAPI call:

<p align="center">
  <img src="docs/sequence.png" alt="Detailed sequence: startup, health check, missing-token rejection, and an authenticated tool call" width="720">
</p>

## Setup

1. **Create an ETAPI token** in Trilium: *Options → ETAPI → Create new ETAPI token*.
   This token is the only credential — the server itself stores no secret. Each
   client presents its own token per request as `Authorization: Bearer <token>`,
   and the server forwards the raw token straight through to Trilium's ETAPI.

   <p align="center">
     <img src="docs/create-etapi.png" alt="Trilium Options → ETAPI screen with the Create new ETAPI token button" width="720">
   </p>

2. **Configure** the deployment via environment variables (see Configuration below) —
   at minimum `TRILIUM_SERVER_URL` pointing at your Trilium instance.
3. **Run** both Trilium and the sidecar:
   ```
   docker compose up -d --build
   ```
   The MCP endpoint is then available at `http://localhost:8081/mcp`.

## Connecting a client

The ETAPI token you create in Trilium (Options → ETAPI) is the credential — pass it in
the `Authorization` header:

```bash
claude mcp add trilium --transport http \
  --header "Authorization: YOUR_TRILIUM_ETAPI_TOKEN" \
  https://your-host/mcp
```

Register multiple instances by repeating with a different URL + token:

```bash
claude mcp add trilium-work --transport http \
  --header "Authorization: WORK_TOKEN" \
  https://work-host/mcp
```

The raw token is what Trilium's ETAPI expects. A `Bearer ` prefix is also accepted (it is
stripped before the request is forwarded), so `Authorization: Bearer YOUR_TOKEN` works too.

All of them use the same trilium-mcp image; each deployment is bound to one Trilium via
`TRILIUM_SERVER_URL`.

On a trusted LAN you can also connect straight to the container over plain HTTP (no reverse
proxy), by IP or hostname:

```bash
claude mcp add trilium --transport http \
  --header "Authorization: YOUR_TRILIUM_ETAPI_TOKEN" \
  http://192.168.1.50:8081/mcp
```

Alternatively, use the provided [`.mcp.json`](.mcp.json), filling in your host and token.

## TLS / reverse proxy

The container serves plain HTTP on `:8081`; terminate TLS at your reverse proxy.
Example Caddyfile:

```
your-host {
    reverse_proxy mcp:8081
}
```

## Configuration

All configuration is via environment variables:

| Variable             | Default               | Purpose                                                                |
| -------------------- | --------------------- | ---------------------------------------------------------------------- |
| `TRILIUM_SERVER_URL` | `http://trilium:8080` | Base URL of the Trilium instance (`/etapi` is appended automatically). |
| `MCP_HOST`           | `0.0.0.0`             | Interface the MCP server binds to.                                     |
| `MCP_PORT`           | `8081`                | Port the MCP server listens on.                                        |
| `MCP_PATH`           | `/mcp`                | HTTP path the MCP endpoint is served at.                               |
| `TRILIUM_ETAPI_SPEC` | bundled spec          | Override the OpenAPI spec path.                                        |
| `MCP_ALLOWED_HOSTS`  | *(unset = any)*       | Comma-separated `Host` allowlist (DNS-rebinding protection). Unset accepts any Host; set it to restrict. |

## Security

The MCP endpoint grants **full read/write access to your notes**. Every request must
carry a valid Trilium ETAPI token in the `Authorization` header; requests with no
`Authorization` header at all are rejected with `401` before reaching any tool. The
server never validates the token itself — validity is enforced by Trilium when the
forwarded request reaches the actual ETAPI call, and the server holds no secret of its
own. The `/health` endpoint is always unauthenticated (used by the container
healthcheck).

The token is sent in the `Authorization` header on every call. Over plain HTTP it travels
in cleartext, so either keep traffic on a **trusted network** (e.g. a LAN or the Docker
network) or put TLS in front — the Caddy reverse proxy above terminates TLS so the token
never crosses an untrusted hop. Direct `http://<lan-ip>:8081` access is fine on a network
you trust.

By default the server accepts requests for **any** `Host` (FastMCP's DNS-rebinding
protection is disabled), so it can be reached by LAN IP or by the domain your reverse proxy
forwards. To lock this down, set `MCP_ALLOWED_HOSTS` to a comma-separated list of the
host[:port] values you actually use (e.g. `192.168.1.50:8081,trilium.example.com`);
`localhost` is always allowed, and anything else gets a `421`.

If the OpenAPI spec cannot be loaded at startup, the server still starts and completes
the MCP handshake, but exposes only a single `startup_error` tool describing how to fix
it (rather than failing with an opaque connection error).

## Layout

```
docker-compose.yaml      trilium + mcp sidecar
Dockerfile               builds the MCP server image (uv-based)
.mcp.json                example MCP client config
app/
  server.py              the MCP server (OpenAPI-driven, HTTP transport, token pass-through)
  pyproject.toml         dependencies
  uv.lock
  trilium-etapi.openapi  bundled Trilium ETAPI OpenAPI spec
```
