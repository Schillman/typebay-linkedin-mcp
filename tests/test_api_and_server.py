import json
import struct
import time

import httpx
import pytest
import respx

from typebay_linkedin_mcp import server, store
from typebay_linkedin_mcp.api import LinkedInClient, LinkedInError
from typebay_linkedin_mcp.safety import Link, ValidationError, validate_images

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
AUTHOR = "urn:li:person:abc"


@pytest.fixture
def img(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_MCP_IMAGE_DIRS", str(tmp_path))
    f = tmp_path / "a.png"
    f.write_bytes(PNG)
    return str(f)


@pytest.fixture
def logged_in(monkeypatch):
    monkeypatch.setattr(store, "get_client", lambda key: None)  # never touch the real keychain
    monkeypatch.setattr(
        store,
        "load_token",
        lambda: {
            "access_token": "SECRET-TOKEN",
            "expires_at": time.time() + 86400 * 30,
            "author_urn": AUTHOR,
            "name": "T",
        },
    )
    server._previews.clear()


@respx.mock
def test_create_post_wire_format(img):
    reg = respx.post("https://api.linkedin.com/v2/assets?action=registerUpload").respond(
        200,
        json={
            "value": {
                "uploadMechanism": {
                    "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest": {
                        "uploadUrl": "https://upload.example/u"
                    }
                },
                "asset": "urn:li:digitalmediaAsset:A1",
            }
        },
    )
    put = respx.put("https://upload.example/u").respond(201)
    route = respx.post("https://api.linkedin.com/v2/ugcPosts").respond(
        201, headers={"x-restli-id": "urn:li:share:99"}
    )
    urn = LinkedInClient(
        "SECRET-TOKEN", AUTHOR, httpx.Client(base_url="https://api.linkedin.com")
    ).create_post("hi (x) [y] @z _w_ #tag", "CONNECTIONS", validate_images([img], ["alt"]))
    assert urn == "urn:li:share:99"
    assert json.loads(reg.calls.last.request.read())["registerUploadRequest"]["owner"] == AUTHOR
    req = route.calls.last.request
    assert req.headers["X-Restli-Protocol-Version"] == "2.0.0"
    assert req.headers["Authorization"] == "Bearer SECRET-TOKEN"
    body = json.loads(req.read())
    content = body["specificContent"]["com.linkedin.ugc.ShareContent"]
    assert content["shareCommentary"]["text"] == "hi (x) [y] @z _w_ #tag"  # sent verbatim
    assert content["shareMediaCategory"] == "IMAGE"
    assert content["media"][0]["media"] == "urn:li:digitalmediaAsset:A1"
    assert content["media"][0]["status"] == "READY"
    assert body["visibility"] == {"com.linkedin.ugc.MemberNetworkVisibility": "CONNECTIONS"}
    assert body["lifecycleState"] == "PUBLISHED" and body["author"] == AUTHOR
    assert put.calls.last.request.headers["Authorization"] == "Bearer SECRET-TOKEN"


@respx.mock
def test_text_only_post_has_no_media():
    route = respx.post("https://api.linkedin.com/v2/ugcPosts").respond(
        201, headers={"x-restli-id": "urn:li:share:1"}
    )
    LinkedInClient("T", AUTHOR, httpx.Client(base_url="https://api.linkedin.com")).create_post(
        "hello", "PUBLIC", []
    )
    content = json.loads(route.calls.last.request.read())["specificContent"]["com.linkedin.ugc.ShareContent"]
    assert content["shareMediaCategory"] == "NONE" and "media" not in content


@respx.mock
def test_401_message_has_no_token():
    respx.post("https://api.linkedin.com/v2/ugcPosts").respond(401, text="nope")
    with pytest.raises(LinkedInError) as e:
        LinkedInClient("SECRET-TOKEN", AUTHOR, httpx.Client(base_url="https://api.linkedin.com")).create_post(
            "x", "PUBLIC", []
        )
    assert "SECRET-TOKEN" not in str(e.value) and "login" in str(e.value)


def _post_error(status, **kw):
    respx.post("https://api.linkedin.com/v2/ugcPosts").respond(status, **kw)
    with pytest.raises(LinkedInError) as e:
        LinkedInClient("T", AUTHOR, httpx.Client(base_url="https://api.linkedin.com")).create_post(
            "x", "PUBLIC", []
        )
    return str(e.value)


@respx.mock
def test_unknown_error_shows_message_not_raw_body():
    msg = _post_error(500, json={"message": "boom", "secretish": "DO-NOT-SHOW"})
    assert "boom" in msg and "DO-NOT-SHOW" not in msg


@respx.mock
def test_non_json_error_body_is_not_echoed():
    msg = _post_error(502, text="<html>DO-NOT-SHOW</html>")
    assert msg == "LinkedIn API error 502." and "DO-NOT-SHOW" not in msg


@respx.mock
def test_410_is_retirement_notice_not_raw_body():
    msg = _post_error(410, json={"message": "gone", "secretish": "DO-NOT-SHOW"})
    assert msg == (
        "LinkedIn retired this endpoint (the UGC Post API is labelled Legacy). "
        "Check for a newer version of typebay-linkedin-mcp."
    )
    assert "DO-NOT-SHOW" not in msg and "gone" not in msg


@respx.mock
def test_422_duplicate_mapping():
    assert "duplicate" in _post_error(422, json={"message": "Duplicate post content"})


@respx.mock
def test_429_throttle_vs_daily_cap():
    assert "1 per minute" in _post_error(429, json={"message": "Comment throttled"})
    assert "150 requests" in _post_error(429, json={"message": "quota"})


def test_publish_requires_matching_preview(logged_in):
    with pytest.raises(ValidationError):  # never previewed
        server.linkedin_publish_post("hello", "deadbeef")
    p = server.linkedin_preview_post("hello")
    with pytest.raises(ValidationError):  # right token, edited text
        server.linkedin_publish_post("hello edited", p["confirm_token"])
    with pytest.raises(ValidationError):  # wrong token
        server.linkedin_publish_post("hello", "deadbeef")


@respx.mock
def test_publish_once_per_preview(logged_in, monkeypatch):
    route = respx.post("https://api.linkedin.com/v2/ugcPosts").respond(
        201, headers={"x-restli-id": "urn:li:share:7"}
    )
    p = server.linkedin_preview_post("hello")
    out = server.linkedin_publish_post("hello", p["confirm_token"])
    assert out["urn"] == "urn:li:share:7" and route.call_count == 1
    with pytest.raises(ValidationError):  # replay
        server.linkedin_publish_post("hello", p["confirm_token"])


def test_expired_preview_and_expired_login(logged_in, monkeypatch):
    p = server.linkedin_preview_post("hello")
    server._previews[p["confirm_token"]] -= 16 * 60
    with pytest.raises(ValidationError):
        server.linkedin_publish_post("hello", p["confirm_token"])
    monkeypatch.setattr(store, "load_token", lambda: {"expires_at": time.time() - 1})
    with pytest.raises(LinkedInError, match="expired"):
        server._client()


def test_delete_requires_confirm(logged_in):
    with pytest.raises(ValidationError):
        server.linkedin_delete_post("urn:li:share:1")


def test_read_only_mode_hides_write_tools(monkeypatch):
    import asyncio
    import importlib

    monkeypatch.setenv("LINKEDIN_MCP_READ_ONLY", "1")
    try:
        ro = importlib.reload(server)
        names = {t.name for t in asyncio.run(ro.mcp.list_tools())}
        assert names == {"linkedin_status", "linkedin_preview_post", "linkedin_list_my_posts"}
    finally:
        monkeypatch.delenv("LINKEDIN_MCP_READ_ONLY")
        importlib.reload(server)


def _link_body(link):
    with respx.mock:
        route = respx.post("https://api.linkedin.com/v2/ugcPosts").respond(
            201, headers={"x-restli-id": "urn:li:share:1"}
        )
        LinkedInClient("T", AUTHOR, httpx.Client(base_url="https://api.linkedin.com")).create_post(
            "hi", "PUBLIC", [], link
        )
        return json.loads(route.calls.last.request.read())


def test_link_post_body_with_title_and_description():
    body = _link_body(Link("https://example.com/a", "T", "D"))
    assert body["specificContent"]["com.linkedin.ugc.ShareContent"] == {
        "shareCommentary": {"text": "hi"},
        "shareMediaCategory": "ARTICLE",
        "media": [
            {
                "status": "READY",
                "originalUrl": "https://example.com/a",
                "title": {"text": "T"},
                "description": {"text": "D"},
            }
        ],
    }


def test_link_post_body_omits_empty_title_and_description():
    media = _link_body(Link("https://example.com/a", "", ""))["specificContent"][
        "com.linkedin.ugc.ShareContent"
    ]["media"]
    assert media == [{"status": "READY", "originalUrl": "https://example.com/a"}]


def test_link_validation(logged_in, img):
    with pytest.raises(ValidationError, match="http"):
        server.linkedin_preview_post("hi", link_url="ftp://example.com")
    with pytest.raises(ValidationError, match="http"):
        server.linkedin_preview_post("hi", link_url="https://")
    with pytest.raises(ValidationError, match="link_url"):
        server.linkedin_preview_post("hi", link_title="T")
    with pytest.raises(ValidationError, match="images or a link"):
        server.linkedin_preview_post("hi", image_paths=[img], link_url="https://example.com")
    with pytest.raises(ValidationError, match="images or a link"):
        server.linkedin_publish_post("hi", "x", image_paths=[img], link_url="https://example.com")


def test_preview_link_output_and_token_binds_link(logged_in):
    p = server.linkedin_preview_post("hi", link_url="https://a.example", link_title=" T ")
    assert p["link"] == {"url": "https://a.example", "title": "T", "description": ""}
    assert "link" not in server.linkedin_preview_post("hi")
    with pytest.raises(ValidationError):
        server.linkedin_publish_post("hi", p["confirm_token"], link_url="https://b.example", link_title="T")


INTROSPECT = "https://www.linkedin.com/oauth/v2/introspectToken"


@pytest.fixture
def with_creds(logged_in, monkeypatch):
    monkeypatch.setattr(store, "get_client", lambda key: f"TOPSECRET-{key}")


@respx.mock
def test_status_active_uses_server_data(with_creds):
    exp = time.time() + 86400 * 30 + 60
    route = respx.post(INTROSPECT).respond(
        200, json={"status": "active", "scope": "w_member_social", "expires_at": exp}
    )
    out = server.linkedin_status()
    assert out == {
        "logged_in": True,
        "name": "T",
        "status": "active",
        "scope": "w_member_social",
        "source": "linkedin",
        "days_left": 30,
    }
    assert route.called and "TOPSECRET" not in json.dumps(out)


@respx.mock
def test_status_revoked_and_renew_hint(with_creds):
    exp = time.time() + 86400 * 3
    respx.post(INTROSPECT).respond(200, json={"status": "revoked", "scope": "s", "expires_at": exp})
    out = server.linkedin_status()
    assert out["logged_in"] is False and out["status"] == "revoked" and out["days_left"] == 2
    assert out["renew"].startswith("Run: typebay-linkedin-mcp login")


@respx.mock
def test_status_network_error_falls_back_to_local(with_creds):
    respx.post(INTROSPECT).mock(side_effect=httpx.ConnectError("boom"))
    out = server.linkedin_status()
    assert out["source"] == "local" and out["logged_in"] is True and out["days_left"] == 29


@respx.mock
def test_status_without_client_creds_makes_no_http_call(logged_in):
    route = respx.post(INTROSPECT).respond(200, json={})
    out = server.linkedin_status()
    assert out["source"] == "local" and not route.called


def test_preview_above_fold_and_no_warnings_when_clean(logged_in):
    p = server.linkedin_preview_post("short hook")
    assert p["above_fold"] == "short hook"
    assert "warnings" not in p


def test_preview_long_text_warns_and_truncates_fold(logged_in):
    p = server.linkedin_preview_post("x" * 300)
    assert p["above_fold"] == "x" * 210
    assert p["warnings"][0].startswith("First line break comes after 300 characters")


def test_preview_metadata_stripped_reported(logged_in, tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_MCP_IMAGE_DIRS", str(tmp_path))
    exif = b"\xff\xe1" + struct.pack(">H", 8) + b"Exif\x00\x00"
    jpg = (
        b"\xff\xd8"
        + exif
        + b"\xff\xdb"
        + struct.pack(">H", 4)
        + b"\x00\x10"
        + b"\xff\xda"
        + struct.pack(">H", 3)
        + b"\x00"
        + b"\x01\x02"
        + b"\xff\xd9"
    )
    f = tmp_path / "e.jpg"
    f.write_bytes(jpg)
    p = server.linkedin_preview_post("hi", image_paths=[str(f)])
    assert p["metadata_stripped"] == [str(f.resolve())]


def test_preview_clean_png_has_no_metadata_stripped(logged_in, img):
    assert "metadata_stripped" not in server.linkedin_preview_post("hi", image_paths=[img])
