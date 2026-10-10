import time

import pytest
import respx

from typebay_linkedin_mcp import postlog, server, store
from typebay_linkedin_mcp.safety import ValidationError


@pytest.fixture
def logged_in(monkeypatch):
    monkeypatch.setattr(store, "get_client", lambda key: None)
    monkeypatch.setattr(
        store,
        "load_token",
        lambda: {
            "access_token": "SECRET-TOKEN",
            "expires_at": time.time() + 86400,
            "author_urn": "urn:li:person:abc",
            "name": "T",
        },
    )
    server._previews.clear()


UGC = "https://api.linkedin.com/v2/ugcPosts"


@pytest.fixture(autouse=True)
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("LINKEDIN_MCP_STATE_DIR", str(tmp_path))
    return tmp_path


def test_record_and_order():
    for n in "abc":
        postlog.record({"urn": n, "ts": 1})
    assert [p["urn"] for p in postlog.posts()] == ["c", "b", "a"]
    assert [p["urn"] for p in postlog.posts(2)] == ["c", "b"]
    assert postlog.path().name == "posts.jsonl"


def test_deleted_filtered():
    postlog.record({"urn": "a", "ts": 1})
    postlog.record({"urn": "b", "ts": 2})
    postlog.record({"deleted": "a", "ts": 3})
    assert [p["urn"] for p in postlog.posts()] == ["b"]
    assert postlog.known("b") and not postlog.known("a") and not postlog.known("z")


def test_default_path_uses_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("LINKEDIN_MCP_STATE_DIR")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert postlog.path() == tmp_path / "typebay-linkedin-mcp" / "posts.jsonl"


def test_list_tool_clamps_limit():
    for i in range(3):
        postlog.record({"urn": str(i), "ts": i})
    assert len(server.linkedin_list_my_posts(limit=0)["posts"]) == 1
    out = server.linkedin_list_my_posts(limit=1000)
    assert len(out["posts"]) == 3 and out["source"] == str(postlog.path())


def _publish(text="hello"):
    tok = server.linkedin_preview_post(text)["confirm_token"]
    return server.linkedin_publish_post(text, tok)


@respx.mock
def test_publish_writes_record(logged_in):
    respx.post(UGC).respond(201, headers={"x-restli-id": "urn:li:share:1"})
    _publish()
    (p,) = postlog.posts()
    assert p["urn"] == "urn:li:share:1" and p["text"] == "hello" and p["link"] is None
    assert p["visibility"] == "PUBLIC" and p["images"] == [] and isinstance(p["ts"], int)
    assert "SECRET-TOKEN" not in postlog.path().read_text()


@respx.mock
def test_publish_survives_log_failure(logged_in, monkeypatch):
    respx.post(UGC).respond(201, headers={"x-restli-id": "urn:li:share:1"})

    def boom(entry):
        raise OSError("disk full")

    monkeypatch.setattr(postlog, "record", boom)
    assert _publish()["published"] is True


@respx.mock
def test_delete_writes_deleted_record(logged_in):
    respx.post(UGC).respond(201, headers={"x-restli-id": "urn:li:share:1"})
    respx.delete(url__regex=r".*ugcPosts.*").respond(204)
    _publish()
    server.linkedin_delete_post("urn:li:share:1", confirm=True)
    assert postlog.posts() == [] and not postlog.known("urn:li:share:1")


def test_delete_unknown_urn_note(logged_in):
    with pytest.raises(ValidationError, match="not in the local post log"):
        server.linkedin_delete_post("urn:li:share:9")
    postlog.record({"urn": "urn:li:share:9", "ts": 1})
    with pytest.raises(ValidationError) as e:
        server.linkedin_delete_post("urn:li:share:9")
    assert "post log" not in str(e.value)


@respx.mock
def test_comment_unknown_urn_note(logged_in):
    respx.post(url__regex=r".*socialActions.*").respond(201, json={})
    assert "not in the local post log" in server.linkedin_add_comment("urn:li:share:9", "hi")["note"]
    postlog.record({"urn": "urn:li:share:9", "ts": 1})
    assert "note" not in server.linkedin_add_comment("urn:li:share:9", "hi")
