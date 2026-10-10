import asyncio
import difflib
import json
import os
from pathlib import Path

from mcp.client import Client

from typebay_linkedin_mcp import server, store

SNAPSHOT = Path(__file__).parent / "__snapshots__" / "tools.json"


def test_tools_snapshot():
    # Taken with read-only OFF (the default); the read-only test reloads and restores server.
    tools = asyncio.run(server.mcp.list_tools())
    actual = json.dumps(
        [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools], indent=2, sort_keys=True
    )
    if os.environ.get("UPDATE_TOOLSNAPS") == "1":
        SNAPSHOT.write_text(actual + "\n")
        return
    expected = SNAPSHOT.read_text().rstrip("\n")
    diff = "\n".join(
        difflib.unified_diff(expected.splitlines(), actual.splitlines(), "snapshot", "actual", lineterm="")
    )
    assert actual == expected, f"Tool surface drifted (UPDATE_TOOLSNAPS=1 to accept):\n{diff}"


def _call_add_comment_logged_out(monkeypatch):
    monkeypatch.setattr(store, "load_token", lambda: None)

    async def go():
        # MCPServer.call_tool raises; the isError result is built by the protocol handler,
        # so go through an in-memory client.
        async with Client(server.mcp) as client:
            return await client.call_tool(
                "linkedin_add_comment", {"post_urn": "urn:li:share:1", "text": "hi"}
            )

    return asyncio.run(go())


def test_linkedin_error_surfaces_as_is_error_result(monkeypatch):
    result = _call_add_comment_logged_out(monkeypatch)
    assert result.is_error is True
    assert "linkedin_add_comment" in result.content[0].text


def test_linkedin_error_message_reaches_client(monkeypatch):
    result = _call_add_comment_logged_out(monkeypatch)
    assert "Not logged in" in result.content[0].text
