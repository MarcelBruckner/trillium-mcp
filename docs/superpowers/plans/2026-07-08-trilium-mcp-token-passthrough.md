# Trilium ETAPI MCP — Header Token Pass-Through Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the standalone Trilium ETAPI MCP server stateless — the client passes its Trilium ETAPI token as `Authorization: Bearer <token>`, the server forwards it to Trilium — and stop rejecting valid ETAPI responses that contain spec-under-specified nulls.

**Architecture:** A pure-ASGI middleware captures the incoming `Authorization` header into a `ContextVar` (401 if absent on the MCP path; `/health` stays open). A dynamic `httpx.Auth` reads that contextvar and forwards the raw token (stripping `Bearer `) to Trilium. `FastMCP.from_openapi(..., validate_output=False)` returns real ETAPI responses. Each container targets one Trilium fixed by `TRILIUM_SERVER_URL`; multi-instance is client-side.

**Tech Stack:** Python ≥3.11, FastMCP 3.4.x, httpx, uvicorn, PyYAML, uv, Docker Compose, pytest (dev only).

## Global Constraints

- Python `requires-python = ">=3.11"`; the container builds on `python3.12`.
- `fastmcp>=3.0.0` (3.4.3 in use), `httpx>=0.28.1`, `pyyaml>=6.0`, `uvicorn>=0.30`.
- The server stores **no** secrets: no `TRILIUM_ETAPI_TOKEN`, no `MCP_AUTH_TOKEN`.
- Trilium's ETAPI expects the **raw** token as the `Authorization` value (apiKey scheme), never a `Bearer ` prefix.
- ETAPI endpoints live under `/etapi`; `TRILIUM_SERVER_URL` (default `http://trilium:8080`) gets `/etapi` appended if absent.
- All app code lives in the single module `app/server.py`; the OpenAPI spec is `app/trillium-etapi.openapi`.
- Pure-ASGI middleware only (never Starlette `BaseHTTPMiddleware` — it buffers and breaks streamable HTTP).

## Preliminaries

The repo has **no commits yet** and is on `main` with the current app untracked. Before Task 1, create a working branch and a baseline commit of the existing state (get the user's OK to commit):

```bash
cd /Users/mbruckner/Repositories/trillium-mcp
git checkout -b feat/token-passthrough
git add -A && git commit -m "chore: baseline standalone trilium-mcp before token pass-through"
```

All later task commits land on this branch.

---

### Task 1: Token-capture ASGI middleware + contextvar

Replaces the current `BearerAuthMiddleware` (which gated on a server-side `MCP_AUTH_TOKEN`). The new middleware requires *any* `Authorization` header on the MCP path and stashes it for the auth flow.

**Files:**
- Modify: `app/server.py` (constants, remove `BearerAuthMiddleware`, add `TokenCaptureMiddleware` + `_incoming_auth`)
- Modify: `app/pyproject.toml` (add pytest dev group)
- Test: `app/tests/test_middleware.py` (create)

**Interfaces:**
- Produces: `server._incoming_auth: ContextVar[str | None]` (default `None`); `server.TokenCaptureMiddleware(app)`; unchanged `server.DEFAULT_PATH = "/mcp"`, `server.HEALTH_PATH = "/health"`.

- [ ] **Step 1: Add pytest dev dependency**

```bash
cd app
uv add --dev pytest
```

- [ ] **Step 2: Write the failing test**

Create `app/tests/test_middleware.py`:

```python
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import server


def _wrapped_app():
    async def echo(request):
        # Echo back whatever token the middleware captured for this request.
        return PlainTextResponse(server._incoming_auth.get() or "")

    async def health(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[
        Route(server.DEFAULT_PATH, echo, methods=["GET", "POST"]),
        Route(server.HEALTH_PATH, health, methods=["GET"]),
    ])
    return server.TokenCaptureMiddleware(app)


def test_missing_auth_on_mcp_returns_401():
    client = TestClient(_wrapped_app())
    r = client.post(server.DEFAULT_PATH)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_health_open_without_auth():
    client = TestClient(_wrapped_app())
    r = client.get(server.HEALTH_PATH)
    assert r.status_code == 200
    assert r.text == "ok"


def test_auth_header_captured_into_contextvar():
    client = TestClient(_wrapped_app())
    r = client.post(server.DEFAULT_PATH, headers={"Authorization": "Bearer abc123"})
    assert r.status_code == 200
    assert r.text == "Bearer abc123"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd app && uv run pytest tests/test_middleware.py -v`
Expected: FAIL — `AttributeError: module 'server' has no attribute 'TokenCaptureMiddleware'` (and `_incoming_auth`).

- [ ] **Step 4: Implement the middleware + contextvar**

In `app/server.py`: add `from contextvars import ContextVar` to the imports. Below the constants block add:

```python
# Per-request holder for the incoming client Authorization header. Populated by
# TokenCaptureMiddleware and read by EtapiTokenAuth when calling Trilium.
_incoming_auth: ContextVar[str | None] = ContextVar("incoming_auth", default=None)
```

Delete the entire `class BearerAuthMiddleware:` and replace with:

```python
class TokenCaptureMiddleware:
    """Pure-ASGI middleware that requires a client Authorization header on the
    MCP endpoint and stashes it for the outgoing ETAPI call.

    The token IS the auth: a request without one is rejected with 401 before it
    reaches FastMCP; validity is enforced by Trilium on the actual ETAPI call.
    Implemented at the ASGI layer (not BaseHTTPMiddleware) so it does not buffer
    the streamable-HTTP response. The health check is always allowed.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # Forward lifespan / websocket scopes untouched.
            await self.app(scope, receive, send)
            return
        if scope.get("path") == HEALTH_PATH:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        authorization = headers.get(b"authorization", b"").decode()
        if not authorization:
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"error":"missing Authorization header"}',
            })
            return
        token = _incoming_auth.set(authorization)
        try:
            await self.app(scope, receive, send)
        finally:
            _incoming_auth.reset(token)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd app && uv run pytest tests/test_middleware.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add app/server.py app/pyproject.toml app/uv.lock app/tests/test_middleware.py
git commit -m "feat: capture client Authorization header via ASGI middleware"
```

---

### Task 2: Dynamic ETAPI auth from the contextvar

`EtapiTokenAuth` no longer takes a token at construction; it reads the per-request contextvar and strips a `Bearer ` prefix before forwarding the raw token to Trilium.

**Files:**
- Modify: `app/server.py` (`EtapiTokenAuth`)
- Test: `app/tests/test_etapi_auth.py` (create)

**Interfaces:**
- Consumes: `server._incoming_auth` (Task 1).
- Produces: `server.EtapiTokenAuth()` — **no constructor args**; `auth_flow` sets `Authorization: <raw token>` on the outgoing request, or raises `RuntimeError` if the contextvar is empty.

- [ ] **Step 1: Write the failing test**

Create `app/tests/test_etapi_auth.py`:

```python
import httpx
import pytest

import server


def _run_auth(header_value):
    reset = server._incoming_auth.set(header_value)
    try:
        auth = server.EtapiTokenAuth()
        request = httpx.Request("GET", "http://trilium:8080/etapi/app-info")
        return next(auth.auth_flow(request))
    finally:
        server._incoming_auth.reset(reset)


def test_strips_bearer_prefix():
    out = _run_auth("Bearer secret-token")
    assert out.headers["Authorization"] == "secret-token"


def test_bearer_prefix_case_insensitive():
    out = _run_auth("bearer secret-token")
    assert out.headers["Authorization"] == "secret-token"


def test_raw_token_passthrough():
    out = _run_auth("raw-token-no-prefix")
    assert out.headers["Authorization"] == "raw-token-no-prefix"


def test_missing_token_raises():
    reset = server._incoming_auth.set(None)
    try:
        auth = server.EtapiTokenAuth()
        request = httpx.Request("GET", "http://trilium:8080/etapi/app-info")
        with pytest.raises(RuntimeError):
            next(auth.auth_flow(request))
    finally:
        server._incoming_auth.reset(reset)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd app && uv run pytest tests/test_etapi_auth.py -v`
Expected: FAIL — `EtapiTokenAuth()` currently requires a `token` argument (`TypeError`).

- [ ] **Step 3: Rewrite `EtapiTokenAuth`**

Replace the existing `class EtapiTokenAuth(httpx.Auth):` block in `app/server.py` with:

```python
class EtapiTokenAuth(httpx.Auth):
    """Forward the client-supplied ETAPI token to Trilium.

    The token arrives per-request in the `_incoming_auth` contextvar (set by
    TokenCaptureMiddleware). Trilium's ETAPI expects the raw token as the
    Authorization value, so we strip a leading 'Bearer ' if the client sent one.
    """

    def auth_flow(self, request: httpx.Request):
        raw = _incoming_auth.get()
        if not raw:
            raise RuntimeError(
                "No client Authorization header available for the ETAPI call."
            )
        if raw[:7].lower() == "bearer ":
            raw = raw[7:].strip()
        request.headers["Authorization"] = raw
        yield request
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd app && uv run pytest tests/test_etapi_auth.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add app/server.py app/tests/test_etapi_auth.py
git commit -m "feat: forward client ETAPI token to Trilium per request"
```

---

### Task 3: Stateless `build_server`, `validate_output=False`, rewired `serve`, trimmed error server

Remove the startup token read, disable output validation (the core bug fix), make the httpx client injectable for testing, always wrap with `TokenCaptureMiddleware`, and simplify the startup-error fallback.

**Files:**
- Modify: `app/server.py` (`build_server`, `serve`, `build_error_server`, remove `token_missing_message` and stale constants)
- Test: `app/tests/test_build_server.py` (create)

**Interfaces:**
- Consumes: `server._incoming_auth`, `server.EtapiTokenAuth`, `server.TokenCaptureMiddleware`.
- Produces: `server.build_server(client: httpx.AsyncClient | None = None) -> FastMCP`; `server.serve(mcp)`; `server.build_error_server(error) -> FastMCP`.

- [ ] **Step 1: Write the failing test**

Create `app/tests/test_build_server.py`:

```python
import asyncio

import httpx

import server


def _mock_client():
    """An httpx client whose transport fakes Trilium's create-note response,
    including branch.prefix == null (the field the spec types as string)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/create-note"):
            return httpx.Response(201, json={
                "note": {
                    "noteId": "abc123", "isProtected": False, "title": "t",
                    "type": "text", "mime": "text/html", "blobId": "b1",
                    "dateCreated": "2026-07-08 00:00:00.000+0000",
                    "dateModified": "2026-07-08 00:00:00.000+0000",
                    "utcDateCreated": "2026-07-08 00:00:00.000Z",
                    "utcDateModified": "2026-07-08 00:00:00.000Z",
                    "parentNoteIds": ["root"], "childNoteIds": [],
                    "parentBranchIds": ["root_abc123"], "childBranchIds": [],
                    "attributes": [],
                },
                "branch": {
                    "branchId": "root_abc123", "noteId": "abc123",
                    "parentNoteId": "root", "prefix": None, "notePosition": 10,
                    "isExpanded": False,
                    "utcDateModified": "2026-07-08 00:00:00.000Z",
                },
            })
        return httpx.Response(404, json={"status": 404})

    return httpx.AsyncClient(
        base_url="http://trilium:8080/etapi",
        auth=server.EtapiTokenAuth(),
        transport=httpx.MockTransport(handler),
    )


def test_build_server_needs_no_token_and_builds_tools():
    mcp = server.build_server()
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert len(tools) >= 30
    assert "createNote" in names


def test_createnote_with_null_prefix_does_not_error():
    from fastmcp import Client

    async def run():
        reset = server._incoming_auth.set("Bearer test-token")
        try:
            mcp = server.build_server(client=_mock_client())
            async with Client(mcp) as c:
                return await c.call_tool("createNote", {
                    "parentNoteId": "root", "title": "t",
                    "type": "text", "content": "c",
                })
        finally:
            server._incoming_auth.reset(reset)

    # Must NOT raise ToolError("Output validation error: None is not of type 'string'").
    result = asyncio.run(run())
    assert result is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd app && uv run pytest tests/test_build_server.py -v`
Expected: FAIL — `build_server()` currently requires the `TRILIUM_ETAPI_TOKEN` env (raises `RuntimeError`) and takes no `client` argument; `createNote` raises the output-validation `ToolError`.

- [ ] **Step 3: Rewrite `build_server`**

Replace the existing `build_server` in `app/server.py` with:

```python
def build_server(client: httpx.AsyncClient | None = None) -> FastMCP:
    """Load the local OpenAPI spec and turn every documented ETAPI endpoint
    into a FastMCP tool. The ETAPI token is supplied per request by the client
    (see TokenCaptureMiddleware / EtapiTokenAuth), so no token is read here.

    `client` is injectable for testing; in production the default client targets
    TRILIUM_SERVER_URL and authenticates from the per-request contextvar.
    """
    if client is None:
        server_url = os.environ.get(SERVER_ENV, DEFAULT_SERVER_URL).rstrip("/")
        # ETAPI endpoints live under /etapi (see the spec's `servers` list).
        if not server_url.endswith("/etapi"):
            server_url = f"{server_url}/etapi"
        client = httpx.AsyncClient(
            base_url=server_url, auth=EtapiTokenAuth(), timeout=60
        )

    spec_path = Path(os.environ.get(SPEC_ENV, str(DEFAULT_SPEC)))
    spec = load_spec(spec_path)

    mcp = FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="Trilium ETAPI MCP",
        # The live ETAPI returns null for fields the spec types as plain
        # strings (e.g. branch.prefix), so response validation would reject
        # otherwise-successful calls. Return the real response instead.
        validate_output=False,
    )
    register_health(mcp)
    return mcp
```

- [ ] **Step 4: Simplify `build_error_server` and `serve`; drop dead code**

In `app/server.py`:

a) Delete the `token_missing_message` function and the now-unused constants `TOKEN_ENV` and `MCP_AUTH_ENV`. Keep `SERVER_ENV`, `SPEC_ENV`, `MCP_HOST_ENV`, `MCP_PORT_ENV`, `MCP_PATH_ENV`, and all `DEFAULT_*` / `HEALTH_PATH` constants.

b) Replace `build_error_server` with (token no longer a startup concern):

```python
def build_error_server(error: BaseException) -> FastMCP:
    """Stand-in MCP server that reports a startup failure over a live
    connection instead of dying with an opaque error. Only reachable now if the
    bundled OpenAPI spec is missing or unparseable.
    """
    summary = str(error).strip() or error.__class__.__name__
    detail = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    ).strip()
    instructions = (
        f"This Trilium ETAPI MCP server FAILED TO START and exposes no Trilium "
        f"tools.\n\nReason: {summary}\n\nThe bundled OpenAPI spec could not be "
        f"loaded. Call the `startup_error` tool for the full error."
    )
    mcp = FastMCP(
        name="Trilium ETAPI MCP (startup failed)",
        instructions=instructions,
    )
    register_health(mcp)

    @mcp.tool
    def startup_error() -> str:
        """Explain why this Trilium ETAPI MCP server failed to start."""
        return (
            "The Trilium ETAPI MCP server failed to start, so no Trilium tools "
            f"are available.\n\n--- Full error ---\n{detail}"
        )

    return mcp
```

c) Replace `serve` with (always wrap; no `MCP_AUTH_TOKEN` branch):

```python
def serve(mcp: FastMCP) -> None:
    """Serve an MCP server over streamable HTTP behind the token-capture
    middleware, using the MCP_* environment configuration."""
    host = os.environ.get(MCP_HOST_ENV, DEFAULT_HOST)
    port = int(os.environ.get(MCP_PORT_ENV, DEFAULT_PORT))
    path = os.environ.get(MCP_PATH_ENV, DEFAULT_PATH)

    app = TokenCaptureMiddleware(mcp.http_app(path=path))

    print(f"Serving Trilium ETAPI MCP on http://{host}:{port}{path} "
          f"(client supplies the ETAPI token via Authorization: Bearer <token>)",
          file=sys.stderr)
    uvicorn.run(app, host=host, port=port)
```

d) Update `main` so the `except` message no longer mentions a server URL/token:

```python
def main():
    try:
        mcp = build_server()
    except Exception as e:
        print(f"Error: failed to build Trilium ETAPI MCP server: {e}",
              file=sys.stderr)
        mcp = build_error_server(e)
    serve(mcp)
```

- [ ] **Step 5: Run the full test suite**

Run: `cd app && uv run pytest -v`
Expected: PASS — all tests in `test_middleware.py`, `test_etapi_auth.py`, `test_build_server.py` green.

- [ ] **Step 6: Commit**

```bash
git add app/server.py app/tests/test_build_server.py
git commit -m "feat: stateless build_server + disable output validation"
```

---

### Task 4: docker-compose + configuration cleanup

Drop server-side secrets from compose and delete the now-pointless `.env.example`.

**Files:**
- Modify: `docker-compose.yaml` (`mcp` service env)
- Delete: `.env.example`
- Modify: `.gitignore` (keep `.env` / `*.token` ignores as defence-in-depth — no change needed if already present)

- [ ] **Step 1: Edit the `mcp` service environment**

In `docker-compose.yaml`, replace the `mcp` service `environment:` block so it contains only the fixed target (remove `TRILIUM_ETAPI_TOKEN` and `MCP_AUTH_TOKEN`):

```yaml
    environment:
      # Fixed target Trilium (reached over the internal compose network).
      # The ETAPI token is supplied per request by the MCP client, not here.
      TRILIUM_SERVER_URL: http://trilium:8080
```

Add a comment above the `ports:` block noting publishing is optional when a reverse proxy on the same Docker network reaches `mcp:8000` directly:

```yaml
    # Optional: only needed if clients/reverse-proxy reach the MCP server from
    # the host. A Caddy container on this network can reach mcp:8000 directly.
    ports:
      - '8000:8000'
```

Leave `build`, `restart`, `depends_on`, and `healthcheck` unchanged.

- [ ] **Step 2: Delete `.env.example`**

```bash
git rm .env.example
```

- [ ] **Step 3: Validate compose**

Run: `docker compose config --quiet && echo OK`
Expected: `OK` (no `TRILIUM_ETAPI_TOKEN` interpolation error, since it's no longer referenced).

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yaml
git commit -m "chore: remove server-side secrets from compose config"
```

---

### Task 5: Documentation (README + `.mcp.json`)

**Files:**
- Modify: `README.md`
- Modify: `.mcp.json`

- [ ] **Step 1: Update `.mcp.json`**

Replace its contents with (the header value is the Trilium ETAPI token):

```json
{
  "mcpServers": {
    "trilium": {
      "type": "http",
      "url": "https://your-host/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_TRILIUM_ETAPI_TOKEN"
      }
    }
  }
}
```

- [ ] **Step 2: Rewrite the README auth/install/deploy sections**

In `README.md`:

a) Replace the **Auth** section with a "Token pass-through" description: the client sends `Authorization: Bearer <ETAPI_TOKEN>`; the server forwards the raw token to Trilium; the server holds no secret. A valid ETAPI token (Trilium → Options → ETAPI) is the only credential.

b) Add an **Install** section:

````markdown
## Connecting a client

The ETAPI token you create in Trilium (Options → ETAPI) is the credential — pass it as a bearer header:

```bash
claude mcp add trilium --transport http \
  --header "Authorization: Bearer YOUR_TRILIUM_ETAPI_TOKEN" \
  https://your-host/mcp
```

Register multiple instances by repeating with a different URL + token:

```bash
claude mcp add trilium-work --transport http \
  --header "Authorization: Bearer WORK_TOKEN" \
  https://work-host/mcp
```

All of them use the same trilium-mcp image; each deployment is bound to one Trilium via `TRILIUM_SERVER_URL`.
````

c) Add a **Reverse proxy (Caddy)** snippet:

````markdown
## TLS / reverse proxy

The container serves plain HTTP on `:8000`; terminate TLS at your reverse proxy. Example Caddyfile:

```
your-host {
    reverse_proxy mcp:8000
}
```
````

d) Update the **Configuration** table: remove `TRILIUM_ETAPI_TOKEN` and `MCP_AUTH_TOKEN`; keep `TRILIUM_SERVER_URL`, `MCP_HOST`, `MCP_PORT`, `MCP_PATH`, `TRILIUM_ETAPI_SPEC`. Update the **Security** section to state the endpoint requires a valid ETAPI token (401 without any `Authorization`), and that the token only travels safely over the Caddy-terminated TLS.

- [ ] **Step 3: Commit**

```bash
git add README.md .mcp.json
git commit -m "docs: token pass-through install, Caddy, multi-instance"
```

---

### Task 6: End-to-end verification against the live stack + cleanup

Prove the whole path against real Trilium and remove test residue. (No pytest — this exercises the running container.)

**Files:** none (verification only).

- [ ] **Step 1: Rebuild and start the stack**

```bash
cd /Users/mbruckner/Repositories/trillium-mcp
docker compose up -d --build
docker compose ps   # both trilium and mcp -> healthy
```

- [ ] **Step 2: Health + missing-auth gate**

```bash
curl -s -o /dev/null -w "health=%{http_code}\n" http://localhost:8000/health          # 200
curl -s -o /dev/null -w "no-auth=%{http_code}\n" -X POST http://localhost:8000/mcp \
  -H 'content-type: application/json' -d '{}'                                          # 401
```

- [ ] **Step 3: Authenticated tool calls (read + write + cleanup)**

Use the real ETAPI token (`TOKEN="$(sed -n 's/^TRILIUM_ETAPI_TOKEN=//p' .env)"` if it still exists, otherwise read it from Trilium → Options → ETAPI). Run a FastMCP client against `http://localhost:8000/mcp` with `auth=f"Bearer {TOKEN}"`:

```bash
cd app
TOKEN="<etapi-token>" uv run python -c "
import os, asyncio
from fastmcp import Client

async def main():
    c = Client('http://localhost:8000/mcp', auth='Bearer ' + os.environ['TOKEN'])
    async with c:
        print('tools:', len(await c.list_tools()))
        print('appinfo ok:', bool((await c.call_tool('getAppInfo', {})).data))
        r = await c.call_tool('createNote', {'parentNoteId':'root','title':'e2e','type':'text','content':'x'})
        nid = r.data.note.noteId
        print('created:', nid)                       # must NOT raise a validation error
        await c.call_tool('deleteNoteById', {'noteId': nid})
        print('deleted:', nid)

asyncio.run(main())
"
```
Expected: `tools: 40`, `appinfo ok: True`, a created note id, then deleted — no `Output validation error`.

- [ ] **Step 4: Wrong-token path**

Repeat Step 3's client with `auth='Bearer wrong'` calling `getAppInfo`; expect an ETAPI **401** surfaced as a tool error.

- [ ] **Step 5: Clean up leftover manual-test notes**

Search for and delete the notes created during earlier manual testing (`mcp-e2e-raw`, and any `mcp-e2e-test`):

```bash
cd app
TOKEN="<etapi-token>" uv run python -c "
import os, asyncio
from fastmcp import Client

async def main():
    c = Client('http://localhost:8000/mcp', auth='Bearer ' + os.environ['TOKEN'])
    async with c:
        for term in ('mcp-e2e-raw','mcp-e2e-test','e2e'):
            res = (await c.call_tool('searchNotes', {'search': term})).data
            for note in getattr(res, 'results', []) or []:
                await c.call_tool('deleteNoteById', {'noteId': note.noteId})
                print('removed', note.noteId, note.title)

asyncio.run(main())
"
```

- [ ] **Step 6: Final commit (if any residue e.g. regenerated lockfiles)**

```bash
git add -A && git commit -m "test: verify token pass-through end-to-end" || echo "nothing to commit"
```

---

## Self-Review

**Spec coverage:**
- Pass-through auth (client Authorization → Trilium) → Tasks 1 + 2.
- No server secrets → Tasks 3 (build_server) + 4 (compose) + 5 (docs).
- One fixed target per process → Task 3 (`TRILIUM_SERVER_URL`), documented Task 5.
- `validate_output=False` bug fix → Task 3 (unit test) + Task 6 (live proof).
- 401 without Authorization, `/health` open → Task 1.
- Caddy reverse proxy / TLS, multi-instance install → Task 5.
- Trimmed startup-error fallback → Task 3.
- Cleanup of leftover test notes → Task 6.

**Placeholder scan:** No TBD/TODO; `YOUR_TRILIUM_ETAPI_TOKEN`, `your-host`, `<etapi-token>` are intentional user-supplied values in docs/verification, not code placeholders.

**Type consistency:** `_incoming_auth` (ContextVar), `TokenCaptureMiddleware(app)`, `EtapiTokenAuth()` (no args), `build_server(client=None)`, `serve(mcp)`, `build_error_server(error)`, constants `DEFAULT_PATH`/`HEALTH_PATH`/`SERVER_ENV`/`SPEC_ENV`/`MCP_*` used consistently across Tasks 1–3. `TOKEN_ENV`/`MCP_AUTH_ENV` are removed in Task 3 and referenced nowhere afterward.
