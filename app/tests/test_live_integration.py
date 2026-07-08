"""Live integration tests against a running Trilium + MCP stack.

These drive the real MCP server over HTTP against a real Trilium ETAPI -- no
mocks. They target the seeded dev fixture from `docker compose up -d` (see
CONTRIBUTING.md): a throwaway Trilium with only the default demo notes.

The whole module is skipped when that stack isn't reachable, so the normal
`uv run pytest` run still passes without Docker. Bring it up first to run them:

    docker compose up -d

Override the endpoint/token with TRILIUM_MCP_URL / TRILIUM_ETAPI_TOKEN.
"""

import asyncio
import os
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

REPO_ROOT = Path(__file__).parents[2]
MCP_URL = os.environ.get("TRILIUM_MCP_URL", "http://localhost:8081/mcp")
HEALTH_URL = str(httpx.URL(MCP_URL).copy_with(path="/health", query=None))


def _token() -> str | None:
    """The ETAPI dev token, from the environment or the committed fixture."""
    tok = os.environ.get("TRILIUM_ETAPI_TOKEN")
    if tok:
        return tok.strip()
    token_file = REPO_ROOT / "etapi.token"
    if token_file.exists():
        return token_file.read_text().strip()
    return None


TOKEN = _token()


def _stack_reachable() -> bool:
    if not TOKEN:
        return False
    try:
        return httpx.get(HEALTH_URL, timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(
    not _stack_reachable(),
    reason=(
        f"live Trilium+MCP stack not reachable at {MCP_URL} "
        f"(run `docker compose up -d`)"
    ),
)


def _client() -> Client:
    return Client(StreamableHttpTransport(MCP_URL, headers={"Authorization": TOKEN}))


def _run(coro):
    return asyncio.run(coro)


def test_live_health_ok():
    assert httpx.get(HEALTH_URL, timeout=5.0).text == "ok"


def test_live_lists_expected_tools():
    async def run():
        async with _client() as c:
            return [t.name for t in await c.list_tools()]

    names = _run(run())
    assert "getAppInfo" in names
    assert "createNote" in names
    # The generated ZIP-export tool is replaced by exactly one custom tool.
    assert names.count("exportNoteSubtree") == 1


def test_live_get_app_info():
    async def run():
        async with _client() as c:
            return await c.call_tool("getAppInfo", {})

    result = _run(run())
    assert isinstance(result.data, dict)
    assert result.data.get("appVersion")


def test_live_export_subtree_returns_readable_text():
    """Regression test for the binary-ZIP crash: exporting the whole tree used
    to raise UnicodeDecodeError; now it returns readable unpacked text."""

    async def run():
        async with _client() as c:
            return await c.call_tool("exportNoteSubtree", {"noteId": "root"})

    result = _run(run())
    text = result.content[0].text
    assert text.startswith("Exported subtree of note 'root'")
    # The export always contains Trilium's metadata file and note sections.
    assert "!!!meta.json" in text
    assert "=====" in text


def test_live_export_subtree_rejects_unknown_format():
    async def run():
        async with _client() as c:
            return await c.call_tool(
                "exportNoteSubtree", {"noteId": "root", "format": "pdf"}
            )

    with pytest.raises(ToolError, match="Unsupported format"):
        _run(run())


def test_live_search_returns_results_list():
    async def run():
        async with _client() as c:
            return await c.call_tool("searchNotes", {"search": "trilium"})

    result = _run(run())
    assert isinstance(result.data, dict)
    assert isinstance(result.data.get("results"), list)


def test_live_note_create_read_delete_round_trip():
    title = "integration-test note"
    body = "hello from the live integration test"

    async def run():
        async with _client() as c:
            created = await c.call_tool(
                "createNote",
                {
                    "parentNoteId": "root",
                    "title": title,
                    "type": "text",
                    "content": body,
                },
            )
            note_id = created.data["note"]["noteId"]
            try:
                got = await c.call_tool("getNoteById", {"noteId": note_id})
                content = await c.call_tool("getNoteContent", {"noteId": note_id})
                return note_id, got.data, content.content[0].text
            finally:
                # Always clean up so the fixture stays pristine.
                await c.call_tool("deleteNoteById", {"noteId": note_id})

    note_id, note, content = _run(run())
    assert note["noteId"] == note_id
    assert note["title"] == title
    assert body in content
