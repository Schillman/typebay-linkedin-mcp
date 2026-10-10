import asyncio

from mcp.client import Client

from typebay_linkedin_mcp import server, store


def test_prompts_listed_with_arguments():
    prompts = asyncio.run(server.mcp.list_prompts())
    got = {p.name: [a.name for a in p.arguments or []] for p in prompts}
    assert got == {
        "announcement": ["topic"],
        "lessons_learned": ["topic"],
        "link_with_first_comment": ["url", "topic"],
    }


def test_link_prompt_content():
    res = asyncio.run(
        server.mcp.get_prompt("link_with_first_comment", {"url": "https://example.com/x", "topic": "Foo"})
    )
    text = res.messages[0].content.text
    assert "https://example.com/x" in text and "Foo" in text
    assert "linkedin_preview_post" in text and "linkedin_list_my_posts" in text
    assert len(text.splitlines()) < 12


def test_status_structured_content_matches_plain_call(monkeypatch):
    monkeypatch.setattr(store, "load_token", lambda: None)

    async def go():
        async with Client(server.mcp) as client:
            return await client.call_tool("linkedin_status", {})

    result = asyncio.run(go())
    assert result.is_error is False
    assert result.structured_content == server.linkedin_status()
