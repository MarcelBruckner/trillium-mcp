import pytest
from fastmcp.exceptions import ToolError

from tests.live._client import client, make_note_pair, path_arg_name, run_async


def _branch_id(data):
    # postBranch returns a Branch object.
    return data["branchId"] if isinstance(data, dict) else None


def test_create_and_get_branch():
    async def run():
        async with client() as c:
            note_a, note_b = await make_note_pair(c)
            created = await c.call_tool(
                "postBranch", {"noteId": note_a, "parentNoteId": note_b}
            )
            branch_id = _branch_id(created.data)
            got = await c.call_tool("getBranchById", {"branchId": branch_id})
            return note_a, note_b, branch_id, got.data
    note_a, note_b, branch_id, branch = run_async(run())
    assert branch_id
    assert branch["noteId"] == note_a
    assert branch["parentNoteId"] == note_b


def test_patch_branch_prefix():
    async def run():
        async with client() as c:
            note_a, note_b = await make_note_pair(c)
            created = await c.call_tool(
                "postBranch", {"noteId": note_a, "parentNoteId": note_b}
            )
            branch_id = _branch_id(created.data)
            arg = await path_arg_name(c, "patchBranchById", "branchId")
            await c.call_tool("patchBranchById", {arg: branch_id, "prefix": "itest-pfx"})
            got = await c.call_tool("getBranchById", {"branchId": branch_id})
            return got.data
    branch = run_async(run())
    assert branch["prefix"] == "itest-pfx"


def test_delete_branch_removes_it():
    async def run():
        async with client() as c:
            note_a, note_b = await make_note_pair(c)
            created = await c.call_tool(
                "postBranch", {"noteId": note_a, "parentNoteId": note_b}
            )
            branch_id = _branch_id(created.data)
            await c.call_tool("deleteBranchById", {"branchId": branch_id})
            with pytest.raises(ToolError):
                await c.call_tool("getBranchById", {"branchId": branch_id})
    run_async(run())
