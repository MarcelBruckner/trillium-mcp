"""Shared helpers for the live integration tests (see conftest.py for the skip)."""

import asyncio
import os
from pathlib import Path

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

# app/tests/live/_client.py -> parents: [live, tests, app, <repo root>]
REPO_ROOT = Path(__file__).resolve().parents[3]
MCP_URL = os.environ.get("TRILIUM_MCP_URL", "http://localhost:8081/mcp")
HEALTH_URL = str(httpx.URL(MCP_URL).copy_with(path="/health", query=None))


def _token() -> str | None:
    tok = os.environ.get("TRILIUM_ETAPI_TOKEN")
    if tok:
        return tok.strip()
    token_file = REPO_ROOT / "etapi.token"
    return token_file.read_text().strip() if token_file.exists() else None


TOKEN = _token()

_reachable: bool | None = None


def stack_reachable() -> bool:
    """True if the MCP /health endpoint answers 200 and we have a token.
    Cached so the network probe runs once per session."""
    global _reachable
    if _reachable is None:
        try:
            _reachable = bool(TOKEN) and (
                httpx.get(HEALTH_URL, timeout=2.0).status_code == 200
            )
        except httpx.HTTPError:
            _reachable = False
    return _reachable


def client() -> Client:
    return Client(StreamableHttpTransport(MCP_URL, headers={"Authorization": TOKEN}))


def run_async(coro):
    return asyncio.run(coro)


async def call(tool: str, args: dict | None = None):
    async with client() as c:
        return await c.call_tool(tool, args or {})


async def make_note(c, *, title="itest", content="body", parent="root", type="text") -> str:
    r = await c.call_tool(
        "createNote",
        {"parentNoteId": parent, "title": title, "type": type, "content": content},
    )
    return r.data["note"]["noteId"]


async def make_note_pair(c) -> tuple[str, str]:
    return await make_note(c, title="itest-A"), await make_note(c, title="itest-B")


async def path_arg_name(c, tool: str, name: str) -> str:
    """The tool's path-param arg for `name`. FastMCP renames a path param to
    `<name>__path` when the request body schema also declares that field
    (e.g. patchNoteById); fall back to `name` when there's no collision."""
    tools = {t.name: t for t in await c.list_tools()}
    props = (tools[tool].inputSchema or {}).get("properties", {})
    return f"{name}__path" if f"{name}__path" in props else name


async def body_arg_name(c, tool: str, path_key: str) -> str:
    """The tool's sole non-path property — used for raw text/plain body tools
    whose body arg name FastMCP derives from the (unnamed) spec schema."""
    tools = {t.name: t for t in await c.list_tools()}
    props = (tools[tool].inputSchema or {}).get("properties", {})
    return next(k for k in props if k != path_key)
