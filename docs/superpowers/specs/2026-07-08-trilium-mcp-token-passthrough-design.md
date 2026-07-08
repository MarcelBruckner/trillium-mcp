# Trilium ETAPI MCP server — header token pass-through

Date: 2026-07-08
Status: Approved (design)

## Context

We built a standalone, containerized MCP server (`app/server.py`) that exposes the Trilium
ETAPI as MCP tools over streamable HTTP, run as a sidecar next to Trilium via
`docker-compose.yaml`. In its current form the server holds **two** secrets in its
environment: the Trilium ETAPI token (`TRILIUM_ETAPI_TOKEN`, forwarded to Trilium) and an
optional self-generated bearer token (`MCP_AUTH_TOKEN`) that gated the MCP endpoint.

Two problems motivated a redesign:

1. **Secret management is awkward and not multi-instance friendly.** Baking the ETAPI token
   into each deployment means the server is bound to one token, and adding a second Trilium
   instance means managing another server-side secret. The user wants to instead register
   several Trilium instances as separate MCP servers in their client, each authenticating with
   that instance's own ETAPI token — passed by the client — so the server needs no baked-in
   secret and the same image is reusable.

2. **A real interop bug.** `FastMCP.from_openapi` validates every tool's *response* against
   the output schema derived from the OpenAPI spec. The live Trilium ETAPI returns `null` for
   fields the spec types as a plain `string` (confirmed: `branch.prefix` is `null` on
   `createNote`), so successful calls fail with `Output validation error: None is not of type
   'string'` even though the write happened. This affects any endpoint whose real response
   includes a nullable field the spec under-specifies.

Intended outcome: a stateless MCP server whose auth is the client-supplied ETAPI token, that
returns real ETAPI responses without spurious validation errors, deployed one-per-instance
behind the user's existing Caddy reverse proxy.

## Requirements

- The client authenticates by sending `Authorization: Bearer <ETAPI_TOKEN>` on every request;
  the server forwards that token to Trilium's ETAPI.
- The server stores **no** ETAPI token and **no** MCP auth token.
- Each running container targets exactly one Trilium instance (fixed at deploy time via
  `TRILIUM_SERVER_URL`). Multi-instance support is entirely client-side: one MCP registration
  per instance, each with its own URL + token.
- Tool calls must return the real ETAPI response body without output-schema validation errors.
- The endpoint runs plain HTTP inside the container; TLS is terminated by the user's Caddy
  reverse proxy.

## Architecture

```
MCP client ──https──▶ Caddy ──http──▶ trilium-mcp:8000 ──http (ETAPI)──▶ trilium:8080
  Authorization: Bearer <ETAPI_TOKEN>          │ forwards raw token as Authorization
                                                ▼
                          one container per Trilium (TRILIUM_SERVER_URL fixed)
```

The MCP handshake (`initialize`, `tools/list`) does not touch Trilium; only tool *calls*
forward to ETAPI. Requiring the `Authorization` header on all `/mcp` requests both gates the
endpoint and supplies the token for calls.

### Components (all in `app/server.py`)

1. **Token-capture ASGI middleware** (`TokenCaptureMiddleware`, replaces the old
   `BearerAuthMiddleware`)
   - Pure ASGI (not Starlette `BaseHTTPMiddleware`, which buffers and breaks streamable HTTP).
   - Forwards non-`http` scopes (lifespan/websocket) untouched.
   - Always allows `GET /health` without a token.
   - For requests to the MCP path: read the `Authorization` header. If absent/empty → respond
     `401` + `WWW-Authenticate: Bearer` and stop. Otherwise store the header value in a
     module-level `ContextVar` for the duration of the request, then call the inner app.

2. **Dynamic ETAPI auth** (`EtapiTokenAuth(httpx.Auth)`, modified)
   - `auth_flow` reads the `ContextVar`, strips a leading `Bearer ` (case-insensitive) if
     present, and sets `request.headers["Authorization"] = <raw token>` on the outgoing ETAPI
     request. Trilium's `EtapiTokenAuth` scheme expects the raw token, not a Bearer prefix.
   - If the contextvar is empty (should not happen behind the middleware) it raises, surfacing
     a clear error rather than calling ETAPI unauthenticated.
   - The single shared `httpx.AsyncClient` is reused across requests; the contextvar makes the
     token per-request/per-task safe.

3. **Server construction** (`build_server`, modified)
   - `FastMCP.from_openapi(openapi_spec=spec, client=client, name=..., validate_output=False)`
     — the key fix: return real ETAPI responses without output-schema validation.
   - No token read at startup. `TRILIUM_SERVER_URL` (default `http://trilium:8080`) still
     resolves the fixed target and `/etapi` is appended if missing.
   - Keep `register_health` (unauthenticated `/health`).

4. **Startup-error fallback** (`build_error_server`, trimmed)
   - Only covers a genuinely unloadable/empty spec now (missing token is no longer a startup
     condition). Keeps completing the MCP handshake and exposing a `startup_error` tool rather
     than dying with an opaque connection error. Wording updated to drop token-fetch hints.

5. **`serve`** (modified) — build `mcp.http_app(path=MCP_PATH)`, always wrap with
   `TokenCaptureMiddleware`, run via `uvicorn`. Remove the `MCP_AUTH_TOKEN` branch.

### Configuration after the change

All non-secret:

| Variable | Default | Purpose |
|----------|---------|---------|
| `TRILIUM_SERVER_URL` | `http://trilium:8080` | Fixed target Trilium (`/etapi` appended). |
| `MCP_HOST` | `0.0.0.0` | Bind interface. |
| `MCP_PORT` | `8000` | Listen port. |
| `MCP_PATH` | `/mcp` | MCP endpoint path. |
| `TRILIUM_ETAPI_SPEC` | bundled spec | Override spec path. |

Removed: `TRILIUM_ETAPI_TOKEN`, `MCP_AUTH_TOKEN`.

### Deployment / docs

- `docker-compose.yaml`: `mcp` service drops the secret env (`TRILIUM_ETAPI_TOKEN`,
  `MCP_AUTH_TOKEN`); keeps `TRILIUM_SERVER_URL: http://trilium:8080`, healthcheck, and
  `depends_on`. Publishing `8000:8000` becomes optional — note that Caddy on the shared Docker
  network can reach `mcp:8000` directly without publishing to the host.
- Delete `.env.example` (no server secrets) and remove the `.env` usage. Keep `.gitignore`
  entries for `.env`/`*.token` as defence-in-depth.
- README: rewrite the auth section for pass-through; add a Caddy `reverse_proxy mcp:8000`
  snippet; document the install command:
  ```
  claude mcp add trilium --transport http \
    --header "Authorization: Bearer YOUR_ETAPI_TOKEN" \
    https://your-host/mcp
  ```
  and show registering multiple instances (different URL + token each).
- `.mcp.json` example updated to show `Authorization: Bearer <ETAPI_TOKEN>` (the ETAPI token,
  not a separate MCP token).

## Error handling

- No `Authorization` on `/mcp` → `401` at the middleware (before touching FastMCP/Trilium).
- Invalid/expired ETAPI token → Trilium returns `401`; the tool call surfaces that error to
  the client. No special handling needed.
- Unloadable/empty spec → `startup_error` fallback server.

## Testing / verification (end-to-end, against the live compose stack)

1. `docker compose up -d --build`; both `trilium` and `mcp` report healthy.
2. `GET /health` (no auth) → `200`.
3. `POST /mcp` without `Authorization` → `401`.
4. Authenticated `tools/list` (client header token) → 40 tools.
5. `getAppInfo`, `searchNotes` → live data.
6. `createNote` → **succeeds** (proves `validate_output=False`); read back with `getNoteById`;
   `deleteNoteById` to clean up; confirm deletion.
7. Wrong token → ETAPI `401` surfaced as a tool error.
8. Clean up the leftover `mcp-e2e-raw` note created during earlier manual testing.

## Out of scope (YAGNI)

- Multi-tenant single-process gateway (one server fronting many Triliums via header/path).
- Per-tool output-schema patching (superseded by disabling output validation wholesale).
- In-container TLS (handled by Caddy).
