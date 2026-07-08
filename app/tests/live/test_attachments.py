import pytest
from fastmcp.exceptions import ToolError

from tests.live._client import body_arg_name, client, make_note, path_arg_name, run_async


async def _make_attachment(c, owner_id, title="itest-att"):
    r = await c.call_tool(
        "postAttachment",
        {
            "ownerId": owner_id,
            "role": "file",
            "mime": "text/plain",
            "title": title,
            "content": "att-body",
        },
    )
    return r.data["attachmentId"], r.data


def test_create_and_get_attachment():
    async def run():
        async with client() as c:
            owner = await make_note(c, title="itest-att-owner")
            att_id, created = await _make_attachment(c, owner, title="itest-att-create")
            got = await c.call_tool("getAttachmentById", {"attachmentId": att_id})
            return att_id, created, got.data
    att_id, created, got = run_async(run())
    assert att_id
    assert created["title"] == "itest-att-create"
    assert got["attachmentId"] == att_id


def test_patch_attachment_title():
    async def run():
        async with client() as c:
            owner = await make_note(c, title="itest-att-owner2")
            att_id, _ = await _make_attachment(c, owner)
            arg = await path_arg_name(c, "patchAttachmentById", "attachmentId")
            await c.call_tool("patchAttachmentById", {arg: att_id, "title": "itest-att-after"})
            got = await c.call_tool("getAttachmentById", {"attachmentId": att_id})
            return got.data
    assert run_async(run())["title"] == "itest-att-after"


def test_put_then_get_attachment_content():
    async def run():
        async with client() as c:
            owner = await make_note(c, title="itest-att-owner3")
            att_id, _ = await _make_attachment(c, owner)
            arg = await body_arg_name(c, "putAttachmentContentById", "attachmentId")
            await c.call_tool("putAttachmentContentById", {"attachmentId": att_id, arg: "att-updated"})
            got = await c.call_tool("getAttachmentContent", {"attachmentId": att_id})
            return got.content[0].text
    assert "att-updated" in run_async(run())


def test_delete_attachment_removes_it():
    async def run():
        async with client() as c:
            owner = await make_note(c, title="itest-att-owner4")
            att_id, _ = await _make_attachment(c, owner)
            await c.call_tool("deleteAttachmentById", {"attachmentId": att_id})
            with pytest.raises(ToolError):
                await c.call_tool("getAttachmentById", {"attachmentId": att_id})
    run_async(run())
