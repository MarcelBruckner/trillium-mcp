import os
import sys
import traceback
from pathlib import Path

import httpx
import uvicorn
import yaml
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

# The ETAPI OpenAPI spec ships alongside this server (baked into the image).
# Tools are generated from it at startup.
DEFAULT_SPEC = Path(__file__).parent / "trillium-etapi.openapi"

# All configuration comes from the environment so the server runs cleanly as a
# container sidecar with no command-line arguments.
TOKEN_ENV = "TRILIUM_ETAPI_TOKEN"          # Trilium ETAPI token (required)
SERVER_ENV = "TRILIUM_SERVER_URL"          # Base URL of the Trilium instance
SPEC_ENV = "TRILIUM_ETAPI_SPEC"            # Override path to the OpenAPI spec
MCP_HOST_ENV = "MCP_HOST"                  # Interface the MCP server binds to
MCP_PORT_ENV = "MCP_PORT"                  # Port the MCP server listens on
MCP_PATH_ENV = "MCP_PATH"                  # HTTP path the MCP endpoint is served at
MCP_AUTH_ENV = "MCP_AUTH_TOKEN"            # Optional bearer token protecting the endpoint

DEFAULT_SERVER_URL = "http://trilium:8080"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
DEFAULT_PATH = "/mcp"
HEALTH_PATH = "/health"


def token_missing_message() -> str:
    """Recovery instructions shown when the ETAPI token is not set."""
    return (
        f"The Trilium ETAPI token is not set. Create a token in Trilium under "
        f"Options -> ETAPI, then provide it to this container via the "
        f"{TOKEN_ENV} environment variable (e.g. in your .env / compose file) "
        f"and restart it."
    )


class EtapiTokenAuth(httpx.Auth):
    """httpx auth flow that attaches the Trilium ETAPI token.

    Trilium's ETAPI expects the raw token as the value of the Authorization
    header (an apiKey scheme, not a 'Bearer' JWT) -- see the EtapiTokenAuth
    security scheme in the OpenAPI spec.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = self._token
        yield request


class BearerAuthMiddleware:
    """Pure-ASGI middleware that gates the MCP endpoint behind a static bearer
    token.

    Implemented at the ASGI layer (not Starlette's BaseHTTPMiddleware) because
    the streamable-HTTP transport uses long-lived streaming responses, which
    BaseHTTPMiddleware buffers and breaks. The health check is always allowed
    so orchestrators can probe liveness without the token.
    """

    def __init__(self, app, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # Forward lifespan / websocket scopes untouched.
            await self.app(scope, receive, send)
            return
        if scope.get("path") == HEALTH_PATH:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"").decode()
        if provided != self._expected:
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
                "body": b'{"error":"unauthorized"}',
            })
            return
        await self.app(scope, receive, send)


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


def build_server() -> FastMCP:
    """Load the local OpenAPI spec and turn every documented ETAPI endpoint
    into a FastMCP tool, calling the instance with the ETAPI token."""
    server_url = os.environ.get(SERVER_ENV, DEFAULT_SERVER_URL)
    base_url = server_url.rstrip("/")
    # ETAPI endpoints live under /etapi (see the spec's `servers` list); the
    # paths in the spec are relative to that.
    if not base_url.endswith("/etapi"):
        base_url = f"{base_url}/etapi"

    token = os.environ.get(TOKEN_ENV)
    if not token:
        raise RuntimeError(token_missing_message())

    spec_path = Path(os.environ.get(SPEC_ENV, str(DEFAULT_SPEC)))
    spec = load_spec(spec_path)

    client = httpx.AsyncClient(
        base_url=base_url, auth=EtapiTokenAuth(token), timeout=60
    )
    mcp = FastMCP.from_openapi(
        openapi_spec=spec,
        client=client,
        name="Trilium ETAPI MCP",
    )
    register_health(mcp)
    return mcp


def build_error_server(error: BaseException) -> FastMCP:
    """Build a minimal stand-in MCP server that reports a startup failure.

    If the real server cannot be built (e.g. the ETAPI token is missing or the
    spec is unreadable), exiting would leave clients with an opaque connection
    error. Instead we start a server that completes the MCP handshake and stays
    alive, announces the reason in its instructions, and returns the full error
    (with recovery steps) via the `startup_error` tool.
    """
    server_url = os.environ.get(SERVER_ENV, DEFAULT_SERVER_URL)
    summary = str(error).strip() or error.__class__.__name__
    detail = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    ).strip()
    fix_hint = (
        "Most likely cause: the ETAPI token is not set. " + token_missing_message()
    )

    instructions = (
        f"This Trilium ETAPI MCP server ({server_url}) FAILED TO START and "
        f"exposes no Trilium tools.\n\nReason: {summary}\n\n{fix_hint}\n\n"
        "Call the `startup_error` tool for the full error and recovery steps."
    )
    mcp = FastMCP(
        name="Trilium ETAPI MCP (startup failed)",
        instructions=instructions,
    )
    register_health(mcp)

    @mcp.tool
    def startup_error() -> str:
        """Explain why this Trilium ETAPI MCP server failed to start, with the
        full error, the affected server URL, and how to recover."""
        return (
            f"The Trilium ETAPI MCP server for {server_url} failed to start, "
            f"so no Trilium tools are available.\n\n"
            f"{fix_hint}\n\n--- Full error ---\n{detail}"
        )

    return mcp


def serve(mcp: FastMCP) -> None:
    """Serve an MCP server over streamable HTTP, optionally behind a bearer
    token, using the MCP_* environment configuration."""
    host = os.environ.get(MCP_HOST_ENV, DEFAULT_HOST)
    port = int(os.environ.get(MCP_PORT_ENV, DEFAULT_PORT))
    path = os.environ.get(MCP_PATH_ENV, DEFAULT_PATH)

    app = mcp.http_app(path=path)

    auth_token = os.environ.get(MCP_AUTH_ENV)
    if auth_token:
        app = BearerAuthMiddleware(app, auth_token)
        print(f"MCP endpoint protected by bearer token ({MCP_AUTH_ENV}).",
              file=sys.stderr)
    else:
        print(f"MCP endpoint is UNAUTHENTICATED -- set {MCP_AUTH_ENV} to "
              f"protect it when reachable beyond localhost.", file=sys.stderr)

    print(f"Serving Trilium ETAPI MCP on http://{host}:{port}{path}",
          file=sys.stderr)
    uvicorn.run(app, host=host, port=port)


def main():
    try:
        mcp = build_server()
    except Exception as e:
        # Don't die: surface the failure over a live connection instead of an
        # opaque error, so clients can call `startup_error` to learn the fix.
        print(f"Error: failed to start Trilium ETAPI MCP server: {e}",
              file=sys.stderr)
        mcp = build_error_server(e)
    serve(mcp)


if __name__ == "__main__":
    main()
