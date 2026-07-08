import json

import pytest
from fastmcp.exceptions import ToolError

from tests.live._client import client, make_note, run_async


def test_create_and_get_note():
    async def run():
        async with client() as c:
            note_id = await make_note(c, title="itest-create")
            got = await c.call_tool("getNoteById", {"noteId": note_id})
            return note_id, got.data
    note_id, note = run_async(run())
    assert note["noteId"] == note_id
    assert note["title"] == "itest-create"


async def _path_arg_name(c, tool: str, name: str) -> str:
    """The tool's argument for a given path parameter.

    Runtime discovery, same rationale as `_body_arg_name` below: `patchNoteById`'s
    request body is the full `Note` schema, which itself has a `noteId` field, so
    FastMCP renames the *path* parameter to `noteId__path` to avoid colliding with
    the body's `noteId` property. Passing plain `noteId` only sets the body field
    and leaves the URL template unsubstituted (a literal 404 on `'{noteId}'`).
    """
    tools = {t.name: t for t in await c.list_tools()}
    props = (tools[tool].inputSchema or {}).get("properties", {})
    suffixed = f"{name}__path"
    return suffixed if suffixed in props else name


def test_patch_note_title():
    async def run():
        async with client() as c:
            note_id = await make_note(c, title="itest-before")
            arg = await _path_arg_name(c, "patchNoteById", "noteId")
            await c.call_tool("patchNoteById", {arg: note_id, "title": "itest-after"})
            got = await c.call_tool("getNoteById", {"noteId": note_id})
            return got.data
    note = run_async(run())
    assert note["title"] == "itest-after"


def test_delete_note_removes_it():
    async def run():
        async with client() as c:
            note_id = await make_note(c, title="itest-delete")
            await c.call_tool("deleteNoteById", {"noteId": note_id})
            # Boundary check: the deleted note is no longer retrievable via the API.
            with pytest.raises(ToolError):
                await c.call_tool("getNoteById", {"noteId": note_id})
    run_async(run())


def test_undelete_note_restores_it():
    async def run():
        async with client() as c:
            note_id = await make_note(c, title="itest-undelete")
            await c.call_tool("deleteNoteById", {"noteId": note_id})
            await c.call_tool("undeleteNote", {"noteId": note_id})
            got = await c.call_tool("getNoteById", {"noteId": note_id})
            return note_id, got.data
    note_id, note = run_async(run())
    assert note["noteId"] == note_id


async def _body_arg_name(c, tool: str) -> str:
    """The single non-path property of a raw-body tool's input schema."""
    tools = {t.name: t for t in await c.list_tools()}
    props = (tools[tool].inputSchema or {}).get("properties", {})
    return next(k for k in props if k != "noteId")


def test_put_then_get_note_content():
    async def run():
        async with client() as c:
            note_id = await make_note(c, title="itest-content", content="orig")
            arg = await _body_arg_name(c, "putNoteContentById")
            await c.call_tool("putNoteContentById", {"noteId": note_id, arg: "updated body"})
            got = await c.call_tool("getNoteContent", {"noteId": note_id})
            return got.content[0].text
    text = run_async(run())
    assert "updated body" in text


def _as_list(data):
    """FastMCP may wrap a top-level array as {"result": [...]} under
    validate_output=False; accept either shape."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("result"), list):
        return data["result"]
    return None


def _result_list(r):
    """`.data` on these three tools is unreliable: their generated output
    schema is `{"type": "object", "additionalProperties": True,
    "x-fastmcp-wrap-result": True}` with no `properties.result`, so the
    fastmcp client's `x-fastmcp-wrap-result` unwrap step hands a bare list to
    a `dict[str, Any]` type-adapter, the validation raises, and `.data` is
    silently left `None` even though the call succeeded. The raw `content`
    text is populated correctly regardless, so fall back to parsing that.
    """
    as_list = _as_list(r.data)
    if as_list is not None:
        return as_list
    return _as_list(json.loads(r.content[0].text))


def test_get_note_revisions_returns_list():
    async def run():
        async with client() as c:
            note_id = await make_note(c, title="itest-revs")
            return await c.call_tool("getNoteRevisions", {"noteId": note_id})
    assert _result_list(run_async(run())) is not None


def test_get_note_attachments_returns_list():
    async def run():
        async with client() as c:
            note_id = await make_note(c, title="itest-atts")
            return await c.call_tool("getNoteAttachments", {"noteId": note_id})
    assert _result_list(run_async(run())) is not None


def test_get_note_history_returns_list():
    r = run_async(_history())
    assert _result_list(r) is not None


async def _history():
    async with client() as c:
        return await c.call_tool("getNoteHistory", {"ancestorNoteId": "root"})


def test_refresh_note_ordering_succeeds():
    async def run():
        async with client() as c:
            return await c.call_tool("postRefreshNoteOrdering", {"parentNoteId": "root"})
    result = run_async(run())
    assert result.is_error is False
