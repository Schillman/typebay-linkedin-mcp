import hashlib

import pytest

from typebay_linkedin_mcp import safety
from typebay_linkedin_mcp.safety import (
    ValidationError,
    comment_token,
    confirm_token,
    lint_text,
    parse_mentions,
    validate_images,
    validate_reaction,
    validate_text,
    validate_video,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 24


def test_text_limits():
    with pytest.raises(ValidationError):
        validate_text("   ")
    with pytest.raises(ValidationError):
        validate_text("x" * 3001)


def test_image_ok_and_token_binds_content(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_MCP_IMAGE_DIRS", str(tmp_path))
    f = tmp_path / "a.png"
    f.write_bytes(PNG)
    imgs = validate_images([str(f)], ["alt"])
    assert imgs[0].mime == "image/png"
    t1 = confirm_token("hello", imgs, "PUBLIC")
    assert t1 != confirm_token("hello!", imgs, "PUBLIC")
    assert t1 != confirm_token("hello", imgs, "CONNECTIONS")
    f.write_bytes(PNG + b"1")
    assert t1 != confirm_token("hello", validate_images([str(f)], ["alt"]), "PUBLIC")


def test_rejects_non_image_outside_dir_and_symlink_escape(tmp_path, monkeypatch):
    allowed = tmp_path / "ok"
    allowed.mkdir()
    monkeypatch.setenv("LINKEDIN_MCP_IMAGE_DIRS", str(allowed))
    secret = tmp_path / "secret.txt"
    secret.write_text("not an image")
    with pytest.raises(ValidationError, match="outside"):
        validate_images([str(secret)])
    (allowed / "link.png").symlink_to(secret)
    with pytest.raises(ValidationError, match="outside"):
        validate_images([str(allowed / "link.png")])
    fake = allowed / "fake.png"
    fake.write_text("text pretending to be png")
    with pytest.raises(ValidationError, match="not a PNG"):
        validate_images([str(fake)])


def test_lint_clean():
    assert lint_text("Hook line\n\nShort body with #one #two.") == []


def test_lint_fold_boundary():
    assert lint_text("x" * 210) == []
    assert lint_text("x" * 210 + "\nmore") == []
    assert lint_text("x" * 211)[0].startswith("First line break comes after 211 characters")
    assert lint_text("x" * 211 + "\nmore")[0].startswith("First line break comes after 211 ")


def test_lint_long_paragraph():
    w = lint_text("hook\n" + "y" * 401)
    assert w == ["Paragraph of 401 characters without a line break."]
    assert lint_text("hook\n" + "y" * 400) == []


def test_lint_hashtags():
    assert lint_text("hi " + " ".join(f"#t{i}" for i in range(5))) == []
    assert lint_text("hi " + " ".join(f"#t{i}" for i in range(6))) == [
        "6 hashtags; 3 to 5 is the usual advice."
    ]


def test_lint_url():
    assert lint_text("see https://example.com") == [
        "URL in the body; consider link_url (preview card) or the first comment."
    ]


@pytest.fixture
def mp4(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_MCP_IMAGE_DIRS", str(tmp_path))
    f = tmp_path / "v.mp4"
    f.write_bytes(MP4)
    return f


def test_video_ok_and_blank_is_none(mp4):
    v = validate_video(str(mp4))
    assert v.sha256 == hashlib.sha256(MP4).hexdigest() and v.size == len(MP4)
    assert validate_video(None) is None and validate_video("  ") is None


def test_video_type_is_magic_bytes_not_extension(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_MCP_IMAGE_DIRS", str(tmp_path))
    f = tmp_path / "x.mp4"
    f.write_bytes(PNG)
    with pytest.raises(ValidationError, match="not an MP4"):
        validate_video(str(f))


def test_video_allowlist_and_size(mp4, tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_MCP_IMAGE_DIRS", str(tmp_path / "other"))
    with pytest.raises(ValidationError, match="outside the allowed"):
        validate_video(str(mp4))
    monkeypatch.setenv("LINKEDIN_MCP_IMAGE_DIRS", str(tmp_path))
    monkeypatch.setattr(safety, "MAX_VIDEO_BYTES", 10)
    with pytest.raises(ValidationError, match="limit is 10"):
        validate_video(str(mp4))


def test_token_binds_video(mp4):
    t0 = confirm_token("hi", [], "PUBLIC")
    t1 = confirm_token("hi", [], "PUBLIC", video=validate_video(str(mp4)))
    assert t0 != t1
    mp4.write_bytes(MP4 + b"1")
    assert t1 != confirm_token("hi", [], "PUBLIC", video=validate_video(str(mp4)))


def test_comment_token_binds_post_and_text():
    t = comment_token("urn:li:share:1", "hi")
    assert t == comment_token("urn:li:share:1", "hi") and len(t) == 16
    assert t != comment_token("urn:li:share:2", "hi") and t != comment_token("urn:li:share:1", "hi!")


def test_plain_token_pinned_and_reshare_changes_it():
    assert (
        confirm_token("hi", [], "PUBLIC")
        == hashlib.sha256(b'{"i": [], "l": null, "t": "hi", "v": "PUBLIC", "vid": null}').hexdigest()[:16]
    )
    assert confirm_token("hi", [], "PUBLIC", reshare_of="urn:li:share:1") != confirm_token("hi", [], "PUBLIC")


def test_validate_reaction():
    assert validate_reaction("like") == "LIKE"
    for bad in ("MAYBE", "", "LOVE"):
        with pytest.raises(
            ValidationError, match="LIKE, PRAISE, EMPATHY, INTEREST, APPRECIATION, ENTERTAINMENT"
        ):
            validate_reaction(bad)


def test_parse_mentions_offsets():
    text, attrs, mentions = parse_mentions(
        "Hi @[Acme](urn:li:organization:1) and @[Bo Li](urn:li:person:Ab9) bye"
    )
    assert text == "Hi Acme and Bo Li bye"
    assert attrs == [
        {
            "start": 3,
            "length": 4,
            "value": {"com.linkedin.common.CompanyAttributedEntity": {"company": "urn:li:organization:1"}},
        },
        {
            "start": 12,
            "length": 5,
            "value": {"com.linkedin.common.MemberAttributedEntity": {"member": "urn:li:person:Ab9"}},
        },
    ]
    assert mentions == [
        {"name": "Acme", "urn": "urn:li:organization:1"},
        {"name": "Bo Li", "urn": "urn:li:person:Ab9"},
    ]
    assert text[3:7] == "Acme" and text[12:17] == "Bo Li"


def test_parse_mentions_no_mentions_and_errors():
    assert parse_mentions("plain @x [y](z)") == ("plain @x [y](z)", [], [])
    with pytest.raises(ValidationError, match="organization and urn:li:person only"):
        parse_mentions("@[X](urn:li:share:1)")
    with pytest.raises(ValidationError, match="display name"):
        parse_mentions("@[](urn:li:person:1)")
