# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A standalone MCP server that turns the [Trilium](https://triliumnotes.org) ETAPI
(External API) into MCP tools. It runs as a **container sidecar** next to a Trilium
instance and exposes ~38 tools generated at startup from the bundled OpenAPI spec,
served over streamable **HTTP**. It stores no secret: each client presents its own
ETAPI token in the `Authorization` header, which is forwarded per-request to Trilium.

The entire server is one module: `app/server.py`.

## Commands

Development uses `uv` (in `app/`) and Docker Compose (at the repo root).

```bash
# Run the full test suite against a fresh, disposable fixture (recommended).
# Resets the seeded DB, rebuilds+starts the stack, waits for health, runs pytest,
# then tears down and resets again — even on failure.
./run-tests.sh
./run-tests.sh tests/live -q     # extra args pass through to pytest
./run-tests.sh -k export         # a single test by keyword

# Manual test run (from app/). Live tests auto-skip when the stack is down.
cd app && uv run pytest
cd app && uv run pytest -k test_strips_bearer_prefix   # single test

# Dev stack: throwaway Trilium (seeded with demo notes) + MCP built from local source.
docker compose up -d --build     # rebuild after any change under app/
curl http://localhost:8081/health   # -> ok

# Reset the seeded fixture to the committed state (containers MUST be down first,
# or SQLite corrupts).
docker compose down && git checkout -- trilium-data/ && docker compose up -d
```

The image is built and pushed to GHCR by `.github/workflows/publish.yml` on push to
`main` and on `v*` tags.

## Test layout

Two kinds of tests, both under `app/tests/`:

- **Unit tests** (`app/tests/test_*.py`) drive `server.py` through a mock httpx
  transport — no running Trilium. These verify the token-forwarding chain
  (`test_etapi_auth`, `test_middleware`, `test_integration`) and the special-cased
  tools (`test_build_server`).
- **Live integration tests** (`app/tests/live/`) drive the real MCP server over HTTP
  against the running stack, covering every MCP tool. They **auto-skip** when the MCP
  `/health` endpoint is unreachable (see `conftest.py` + `_client.stack_reachable`).
  Note: live tests mutate the fixture, so reset `trilium-data/` afterwards (or just use
  `run-tests.sh`).

`test_coverage.py` is a **guard**: it fails if any ETAPI `operationId` (minus the
excluded `login`/`logout`) lacks a live test, and if tests reference unknown tool
names. When you add or special-case a tool, add a matching live test or the guard
fails CI.

## Architecture

`build_server()` in `app/server.py` is the core. It calls `FastMCP.from_openapi` on
the bundled `app/trilium-etapi.openapi` spec, generating one tool per ETAPI endpoint.
The critical detail is that **several endpoints don't fit FastMCP's JSON-in/JSON-out
assumption and each fails at a different layer**, so they get different fixes:

- **text/html RESPONSES** (e.g. `getNoteContent`) — a *metadata* problem. The generated
  tool returns the right text, but leaves an output schema attached that the MCP layer
  rejects. Fixed by clearing the schema via `mcp_component_fn=drop_non_json_output_schema`.
- **application/zip RESPONSE** (`exportNoteSubtree`) — a *behavior* problem. ZIP bytes make
  `response.json()` raise an uncaught `UnicodeDecodeError`. The generated tool is
  **excluded** (RouteMap) and **replaced** by `register_export_tool`, which fetches, unzips,
  and returns readable text (capped at `MAX_EXPORT_CHARS`).
- **text/plain REQUEST bodies** (`putNoteContentById`, `putAttachmentContentById`) — a
  *behavior* problem. FastMCP's director sends a scalar body with no Content-Type, so
  Trilium 500s. Excluded and **replaced** by `register_content_put_tools`, which sends the
  body with an explicit `text/plain; charset=utf-8` Content-Type.
- `login`/`logout` are **excluded outright** (RouteMap, no replacement): they manage session
  tokens, but an MCP client already authenticates via the header, and logout would invalidate
  its own credential.

`mcp_component_fn` can only adjust metadata, which is why the behavior cases use
exclusion + replacement rather than a component tweak.

Also note `validate_output=False`: the live ETAPI returns `null` for fields the spec
types as plain strings (e.g. `branch.prefix`), so output validation would reject
otherwise-successful calls.

### Token pass-through (the auth model)

The server holds no secret. The ETAPI token travels per-request through a contextvar:

1. `TokenCaptureMiddleware` (pure-ASGI, not `BaseHTTPMiddleware`, so it doesn't buffer the
   streamable-HTTP response) requires an `Authorization` header on the MCP path, rejecting
   requests without one as `401` before FastMCP sees them. It stashes the header in the
   `_incoming_auth` contextvar. `/health` is always allowed through unauthenticated.
2. `EtapiTokenAuth` (an `httpx.Auth`) reads `_incoming_auth` on the outgoing ETAPI call,
   strips a leading `Bearer ` if present, and sets it as the raw `Authorization` value
   Trilium expects.

Validity is enforced by Trilium when the forwarded request arrives — this server never
validates the token itself.

### Startup resilience

If the OpenAPI spec can't be loaded, `main()` falls back to `build_error_server()`, which
completes the MCP handshake but exposes only a `startup_error` tool describing the failure —
instead of dying with an opaque connection error.

### Configuration

All config is environment variables (no CLI args), so the server runs cleanly as a sidecar:
`TRILIUM_SERVER_URL` (`/etapi` is appended automatically), `MCP_HOST`, `MCP_PORT`, `MCP_PATH`,
`TRILIUM_ETAPI_SPEC`, `MCP_ALLOWED_HOSTS`. Host protection (DNS-rebinding) is **off by
default** (any Host accepted; the token is the real gate) and only restricts when
`MCP_ALLOWED_HOSTS` is set.

## Dev fixture credentials

The seeded Trilium at http://localhost:8080 uses password `trilium-mcp`; the ETAPI token
is in `etapi.token`. Both are **committed dev fixtures** scoped to the disposable instance —
never reuse them anywhere real. The seeded DB lives in `trilium-data/` (only Trilium's
default demo notes).

## Diagrams

`docs/*.puml` render to PNGs via the `.githooks/pre-commit` hook (enable once with
`git config core.hooksPath .githooks`; requires `plantuml`). The hook re-renders and stages
any PNG that's missing or older than its `.puml`/`theme.iuml`, so a commit touching a diagram
always carries a matching PNG.
