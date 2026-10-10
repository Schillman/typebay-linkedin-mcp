import json
import struct
import time

import httpx
import pytest
import respx

from typebay_linkedin_mcp import api, server, store
from typebay_linkedin_mcp.api import LinkedInClient, LinkedInError, LinkedInUnknownOutcome
from typebay_linkedin_mcp.safety import (
    Link,
    ValidationError,
    confirm_token,
    validate_images,
    validate_video,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
AUTHOR = "urn:li:person:abc"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 24


@pytest.fixture
def img(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_MCP_IMAGE_DIRS", str(tmp_path))
    f = tmp_path / "a.png"
    f.write_bytes(PNG)
    return str(f)


@pytest.fixture
def mp4(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_MCP_IMAGE_DIRS", str(tmp_path))
    f = tmp_path / "clip.mp4"
    f.write_bytes(MP4)
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
        assert names == {
            "linkedin_status",
            "linkedin_preview_post",
            "linkedin_list_my_posts",
            "linkedin_preview_comment",
        }
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
    with pytest.raises(ValidationError, match="only one of them"):
        server.linkedin_preview_post("hi", image_paths=[img], link_url="https://example.com")
    with pytest.raises(ValidationError, match="only one of them"):
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


def _video_routes(statuses):
    reg = respx.post("https://api.linkedin.com/v2/assets?action=registerUpload").respond(
        200,
        json={
            "value": {
                "uploadMechanism": {
                    "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest": {
                        "uploadUrl": "https://upload.example/v"
                    }
                },
                "asset": "urn:li:digitalmediaAsset:V1",
            }
        },
    )
    put = respx.put("https://upload.example/v").respond(201)
    status = respx.get("https://api.linkedin.com/v2/assets/V1").mock(
        side_effect=[httpx.Response(200, json={"recipes": [{"status": s}]}) for s in statuses]
    )
    post = respx.post("https://api.linkedin.com/v2/ugcPosts").respond(
        201, headers={"x-restli-id": "urn:li:ugcPost:7"}
    )
    return reg, put, status, post


@respx.mock
def test_video_post_wire_format(mp4, monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    reg, put, status, post = _video_routes(["NEW", "AVAILABLE"])
    urn = LinkedInClient(
        "SECRET-TOKEN", AUTHOR, httpx.Client(base_url="https://api.linkedin.com")
    ).create_post("hi", "PUBLIC", [], video=validate_video(mp4))
    assert urn == "urn:li:ugcPost:7"
    assert (
        "feedshare-video" in json.loads(reg.calls.last.request.read())["registerUploadRequest"]["recipes"][0]
    )
    assert put.calls.last.request.headers["Authorization"] == "Bearer SECRET-TOKEN"
    assert status.call_count == 2
    content = json.loads(post.calls.last.request.read())["specificContent"]["com.linkedin.ugc.ShareContent"]
    assert content["shareMediaCategory"] == "VIDEO"
    assert content["media"] == [
        {"status": "READY", "media": "urn:li:digitalmediaAsset:V1", "title": {"text": "clip"}}
    ]


@respx.mock
def test_video_processing_error_never_posts(mp4, monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    _, _, _, post = _video_routes(["CLIENT_ERROR"])
    client = LinkedInClient("SECRET-TOKEN", AUTHOR, httpx.Client(base_url="https://api.linkedin.com"))
    with pytest.raises(LinkedInError, match="could not process") as e:
        client.create_post("hi", "PUBLIC", [], video=validate_video(mp4))
    assert "SECRET-TOKEN" not in str(e.value)
    assert not post.called


@respx.mock
def test_video_poll_deadline_never_posts(mp4, monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    monkeypatch.setattr(api, "POLL_DEADLINE", 0)
    _, _, _, post = _video_routes(["NEW", "NEW"])
    client = LinkedInClient("SECRET-TOKEN", AUTHOR, httpx.Client(base_url="https://api.linkedin.com"))
    with pytest.raises(LinkedInError, match="still processing"):
        client.create_post("hi", "PUBLIC", [], video=validate_video(mp4))
    assert not post.called


def test_video_exclusive_with_images_and_link(logged_in, img, mp4):
    with pytest.raises(ValidationError, match="only one of them"):
        server.linkedin_preview_post("hi", image_paths=[img], video_path=mp4)
    with pytest.raises(ValidationError, match="only one of them"):
        server.linkedin_preview_post("hi", link_url="https://example.com", video_path=mp4)


@respx.mock
def test_preview_reports_video_and_publish_logs_it(logged_in, mp4, monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    _video_routes(["AVAILABLE"])
    p = server.linkedin_preview_post("hi", video_path=mp4)
    assert p["video"].endswith("clip.mp4") and "video" not in server.linkedin_preview_post("hi")
    out = server.linkedin_publish_post("hi", p["confirm_token"], video_path=mp4)
    assert out["urn"] == "urn:li:ugcPost:7"
    assert server.linkedin_list_my_posts()["posts"][0]["video"] == p["video"]


def _state_log(tmp_path):
    f = tmp_path / "posts.jsonl"
    return f.read_text() if f.exists() else ""


@respx.mock
def test_publish_timeout_is_unknown_outcome(logged_in, tmp_path):
    respx.post("https://api.linkedin.com/v2/ugcPosts").mock(side_effect=httpx.ConnectTimeout("x"))
    p = server.linkedin_preview_post("hello")
    with pytest.raises(LinkedInUnknownOutcome, match="may still have been published"):
        server.linkedin_publish_post("hello", p["confirm_token"])
    assert p["confirm_token"] not in server._previews
    assert '"unknown_outcome": true' in _state_log(tmp_path)
    assert server.postlog.posts() == []


@respx.mock
def test_upload_timeout_keeps_preview_and_logs_nothing(logged_in, img, tmp_path):
    respx.post("https://api.linkedin.com/v2/assets?action=registerUpload").mock(
        side_effect=httpx.ReadTimeout("x")
    )
    p = server.linkedin_preview_post("hello", image_paths=[img])
    with pytest.raises(LinkedInError) as ei:
        server.linkedin_publish_post("hello", p["confirm_token"], image_paths=[img])
    assert not isinstance(ei.value, LinkedInUnknownOutcome)
    assert p["confirm_token"] in server._previews
    assert _state_log(tmp_path) == ""


def test_preview_prunes_expired_tokens(logged_in):
    server._previews["stale"] = time.time() - server.CONFIRM_TTL_SECONDS - 1
    p = server.linkedin_preview_post("hello")
    assert "stale" not in server._previews and p["confirm_token"] in server._previews


def _client_for_tests():
    return LinkedInClient("SECRET-TOKEN", AUTHOR, httpx.Client(base_url="https://api.linkedin.com"))


SOCIAL = "https://api.linkedin.com/v2/socialActions/urn%3Ali%3Ashare%3A5/comments"


@respx.mock
def test_comment_needs_preview_and_token_is_single_use(logged_in):
    route = respx.post(SOCIAL).respond(201, json={})
    with pytest.raises(ValidationError, match="Call linkedin_preview_comment again"):
        server.linkedin_add_comment("urn:li:share:5", "hi", "deadbeef")
    p = server.linkedin_preview_comment("urn:li:share:5", "hi")
    assert p["preview"] == "hi" and "not in the local post log" in p["note"]
    with pytest.raises(ValidationError):  # edited text
        server.linkedin_add_comment("urn:li:share:5", "hi!", p["confirm_token"])
    with pytest.raises(ValidationError):  # other post
        server.linkedin_add_comment("urn:li:share:6", "hi", p["confirm_token"])
    assert not route.called
    server.linkedin_add_comment("urn:li:share:5", "hi", p["confirm_token"])
    assert json.loads(route.calls.last.request.read()) == {"actor": AUTHOR, "message": {"text": "hi"}}
    with pytest.raises(ValidationError):  # replay
        server.linkedin_add_comment("urn:li:share:5", "hi", p["confirm_token"])
    assert route.call_count == 1


@respx.mock
def test_stale_comment_preview_refused(logged_in):
    route = respx.post(SOCIAL).respond(201, json={})
    p = server.linkedin_preview_comment("urn:li:share:5", "hi")
    server._previews[p["confirm_token"]] -= server.CONFIRM_TTL_SECONDS + 1
    with pytest.raises(ValidationError, match="does not match a recent preview"):
        server.linkedin_add_comment("urn:li:share:5", "hi", p["confirm_token"])
    assert not route.called


LIKES = "https://api.linkedin.com/v2/socialActions/urn%3Ali%3Ashare%3A5/likes"


@respx.mock
def test_like_wire_format():
    route = respx.post(LIKES).respond(201)
    _client_for_tests().like("urn:li:share:5")
    req = route.calls.last.request
    assert req.method == "POST" and str(req.url) == LIKES
    assert "Linkedin-Version" not in req.headers
    assert req.headers["X-Restli-Protocol-Version"] == "2.0.0"
    assert req.headers["Authorization"] == "Bearer SECRET-TOKEN"
    assert json.loads(req.read()) == {"actor": AUTHOR, "object": "urn:li:share:5"}


@respx.mock
def test_unlike_wire_format():
    url = f"{LIKES}/urn%3Ali%3Aperson%3Aabc?actor=urn%3Ali%3Aperson%3Aabc"
    route = respx.delete(url__regex=r".*/likes/.*").respond(204)
    _client_for_tests().unlike("urn:li:share:5")
    req = route.calls.last.request
    assert req.method == "DELETE" and str(req.url) == url
    assert "Linkedin-Version" not in req.headers
    assert req.headers["Authorization"] == "Bearer SECRET-TOKEN"


@respx.mock
def test_react_tool_likes_and_notes_unknown_urn(logged_in):
    route = respx.post(LIKES).respond(201)
    out = server.linkedin_react_to_post("urn:li:share:5")
    assert out["reacted"] is True and out["reaction"] == "LIKE" and "post log" in out["note"]
    assert json.loads(route.calls.last.request.read()) == {"actor": AUTHOR, "object": "urn:li:share:5"}
    unr = respx.delete(url__regex=r".*/likes/.*").respond(204)
    out = server.linkedin_react_to_post("urn:li:share:5", remove=True)
    assert out["reacted"] is False and unr.call_count == 1


@respx.mock
def test_403_message_explains_scopes():
    msg = _post_error(403, json={"message": "DO-NOT-SHOW"})
    assert msg.startswith("LinkedIn denied the request (403). The token's scopes do not cover")
    assert "connections-only" in msg and "Share on LinkedIn product" in msg and "DO-NOT-SHOW" not in msg


@respx.mock
def test_reshare_wire_format_and_token(logged_in):
    route = respx.post("https://api.linkedin.com/v2/ugcPosts").respond(
        201, headers={"x-restli-id": "urn:li:share:8"}
    )
    p = server.linkedin_preview_post("nice", reshare_of="urn:li:share:5")
    assert p["reshare_of"] == "urn:li:share:5"
    assert p["confirm_token"] != server.linkedin_preview_post("nice")["confirm_token"]
    with pytest.raises(ValidationError):  # token binds the reshare target
        server.linkedin_publish_post("nice", p["confirm_token"], reshare_of="urn:li:share:6")
    server.linkedin_publish_post("nice", p["confirm_token"], reshare_of="urn:li:share:5")
    body = json.loads(route.calls.last.request.read())
    assert body["responseContext"] == {"parent": "urn:li:share:5"}
    content = body["specificContent"]["com.linkedin.ugc.ShareContent"]
    assert content["shareMediaCategory"] == "NONE" and content["shareCommentary"] == {"text": "nice"}
    assert server.linkedin_list_my_posts()["posts"][0]["reshare_of"] == "urn:li:share:5"


def test_plain_post_has_no_response_context_and_token_stable():
    assert confirm_token("hi", [], "PUBLIC") == confirm_token("hi", [], "PUBLIC", reshare_of=None)
    with respx.mock:
        route = respx.post("https://api.linkedin.com/v2/ugcPosts").respond(
            201, headers={"x-restli-id": "urn:li:share:1"}
        )
        _client_for_tests().create_post("hi", "PUBLIC", [])
    assert "responseContext" not in json.loads(route.calls.last.request.read())


def test_reshare_validation(logged_in, img):
    with pytest.raises(ValidationError, match="only one of them"):
        server.linkedin_preview_post("hi", image_paths=[img], reshare_of="urn:li:share:5")
    with pytest.raises(ValidationError, match="only one of them"):
        server.linkedin_preview_post("hi", link_url="https://a.example", reshare_of="urn:li:share:5")
    with pytest.raises(ValidationError, match="only one of them"):
        server.linkedin_publish_post("hi", "x", image_paths=[img], reshare_of="urn:li:share:5")
    for bad in ("urn:li:person:abc", "https://linkedin.com/x", "share:5"):
        with pytest.raises(ValidationError, match="reshare_of"):
            server.linkedin_preview_post("hi", reshare_of=bad)
    assert server.linkedin_preview_post("hi", reshare_of="urn:li:activity:5")["reshare_of"]


EDIT = "https://api.linkedin.com/rest/posts/urn%3Ali%3Ashare%3A5"


@respx.mock
def test_edit_requires_confirm_and_sends_nothing(logged_in, tmp_path):
    route = respx.post(EDIT).respond(204)
    with pytest.raises(ValidationError, match="replaces its text") as e:
        server.linkedin_edit_post("urn:li:share:5", "new")
    assert "post log" in str(e.value) and not route.called
    assert _state_log(tmp_path) == ""


@respx.mock
def test_edit_wire_format_and_log(logged_in, tmp_path):
    route = respx.post(EDIT).respond(204)
    server.postlog.record({"urn": "urn:li:share:5", "text": "old", "ts": 1})
    out = server.linkedin_edit_post("urn:li:share:5", " new text ", confirm=True)
    assert out["edited"] is True and out["url"].endswith("urn:li:share:5/") and "note" not in out
    req = route.calls.last.request
    assert req.headers["X-RestLi-Method"] == "PARTIAL_UPDATE"
    assert req.headers["Linkedin-Version"] == "202609"
    assert req.headers["X-Restli-Protocol-Version"] == "2.0.0"
    assert json.loads(req.read()) == {"patch": {"$set": {"commentary": "new text"}}}
    entry = json.loads(_state_log(tmp_path).splitlines()[-1])
    assert entry["edited"] == "urn:li:share:5" and entry["text"] == "new text"
    assert server.postlog.posts()[0]["text"] == "old"  # the original record is untouched


@respx.mock
def test_mention_publish_sends_plain_text_and_attributes(logged_in):
    route = respx.post("https://api.linkedin.com/v2/ugcPosts").respond(
        201, headers={"x-restli-id": "urn:li:share:9"}
    )
    raw = "Thanks @[Acme Corp](urn:li:organization:123) and @[Jo](urn:li:person:AbC)!"
    p = server.linkedin_preview_post(raw)
    assert p["preview"] == "Thanks Acme Corp and Jo!"
    assert p["mentions"] == [
        {"name": "Acme Corp", "urn": "urn:li:organization:123"},
        {"name": "Jo", "urn": "urn:li:person:AbC"},
    ]
    assert p["confirm_token"] == confirm_token(raw, [], "PUBLIC")  # token binds the raw text
    server.linkedin_publish_post(raw, p["confirm_token"])
    content = json.loads(route.calls.last.request.read())["specificContent"]["com.linkedin.ugc.ShareContent"]
    assert content["shareCommentary"] == {
        "text": "Thanks Acme Corp and Jo!",
        "attributes": [
            {
                "start": 7,
                "length": 9,
                "value": {
                    "com.linkedin.common.CompanyAttributedEntity": {"company": "urn:li:organization:123"}
                },
            },
            {
                "start": 21,
                "length": 2,
                "value": {"com.linkedin.common.MemberAttributedEntity": {"member": "urn:li:person:AbC"}},
            },
        ],
    }
    assert server.linkedin_list_my_posts()["posts"][0]["text"] == "Thanks Acme Corp and Jo!"
