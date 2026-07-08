import os
import sys
import traceback
from contextvars import ContextVar
from pathlib import Path

import httpx
import uvicorn
import yaml
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

# The ETAPI OpenAPI spec ships alongside this server (baked into the image).
# Tools are generated from it at startup.
DEFAULT_SPEC = Path(__file__).parent / "trilium-etapi.openapi"

# All configuration comes from the environment so the server runs cleanly as a
# container sidecar with no command-line arguments.
SERVER_ENV = "TRILIUM_SERVER_URL"          # Base URL of the Trilium instance
SPEC_ENV = "TRILIUM_ETAPI_SPEC"            # Override path to the OpenAPI spec
MCP_HOST_ENV = "MCP_HOST"                  # Interface the MCP server binds to
MCP_PORT_ENV = "MCP_PORT"                  # Port the MCP server listens on
MCP_PATH_ENV = "MCP_PATH"                  # HTTP path the MCP endpoint is served at
MCP_ALLOWED_HOSTS_ENV = "MCP_ALLOWED_HOSTS"  # comma-separated Host allowlist (see serve)

DEFAULT_SERVER_URL = "http://trilium:8080"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8081
DEFAULT_PATH = "/mcp"
HEALTH_PATH = "/health"

# Per-request holder for the incoming client Authorization header. Populated by
# TokenCaptureMiddleware and read by EtapiTokenAuth when calling Trilium.
_incoming_auth: ContextVar[str | None] = ContextVar("incoming_auth", default=None)


class EtapiTokenAuth(httpx.Auth):
    """Forward the client-supplied ETAPI token to Trilium.

    The token arrives per-request in the `_incoming_auth` contextvar (set by
    TokenCaptureMiddleware). Trilium's ETAPI expects the raw token as the
    Authorization value, so we strip a leading 'Bearer ' if the client sent one.
    """

    def auth_flow(self, request: httpx.Request):
        raw = _incoming_auth.get()
        if raw and raw[:7].lower() == "bearer ":
            raw = raw[7:].strip()
        if not raw:
            raise RuntimeError(
                "No client Authorization header available for the ETAPI call."
            )
        request.headers["Authorization"] = raw
        yield request


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


def load_spec(spec_path: Path) -> dict:
    """Parse the on-disk ETAPI OpenAPI spec into a dict.

    The spec ships as YAML; because YAML is a superset of JSON this also parses
    a JSON spec, so the file can be swapped for either format.
    """
    if not spec_path.exists():
        raise RuntimeError(f"OpenAPI spec not found at {spec_path}.")
    text = spec_path.read_text()
    if not text.strip():
        raise RuntimeError(
            f"OpenAPI spec at {spec_path} is empty -- populate it with the "
            f"Trilium ETAPI OpenAPI spec."
        )
    spec = yaml.safe_load(text)
    if not isinstance(spec, dict):
        raise RuntimeError(f"OpenAPI spec at {spec_path} is not a valid mapping.")
    return spec


def register_health(mcp: FastMCP) -> None:
    """Add an unauthenticated health endpoint for container healthchecks."""

    @mcp.custom_route(HEALTH_PATH, methods=["GET"])
    async def health(_request: Request):
        return PlainTextResponse("ok")


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


def serve(mcp: FastMCP) -> None:
    """Serve an MCP server over streamable HTTP behind the token-capture
    middleware, using the MCP_* environment configuration."""
    host = os.environ.get(MCP_HOST_ENV, DEFAULT_HOST)
    port = int(os.environ.get(MCP_PORT_ENV, DEFAULT_PORT))
    path = os.environ.get(MCP_PATH_ENV, DEFAULT_PATH)

    # FastMCP's streamable-HTTP transport does DNS-rebinding protection: by
    # default it 421s any Host header that isn't localhost. This server is meant
    # to be reached by LAN IP or (behind a reverse proxy) a public domain, and
    # the ETAPI token is the real gate -- so leave the Host allowlist open by
    # default and only restrict when MCP_ALLOWED_HOSTS is set.
    allowed = os.environ.get(MCP_ALLOWED_HOSTS_ENV, "").strip()
    if allowed:
        hosts = [h.strip() for h in allowed.split(",") if h.strip()]
        inner = mcp.http_app(path=path, allowed_hosts=hosts)
        print(f"Host protection ON; allowed hosts (plus localhost): {hosts}",
              file=sys.stderr)
    else:
        inner = mcp.http_app(path=path, host_origin_protection=False)
        print(f"Host protection OFF (any Host accepted) -- set "
              f"{MCP_ALLOWED_HOSTS_ENV} to restrict.", file=sys.stderr)
    app = TokenCaptureMiddleware(inner)

    print(f"Serving Trilium ETAPI MCP on http://{host}:{port}{path} "
          f"(client supplies the ETAPI token via the Authorization header)",
          file=sys.stderr)
    uvicorn.run(app, host=host, port=port)


def main():
    try:
        mcp = build_server()
    except Exception as e:
        print(f"Error: failed to build Trilium ETAPI MCP server: {e}",
              file=sys.stderr)
        mcp = build_error_server(e)
    serve(mcp)


if __name__ == "__main__":
    main()
