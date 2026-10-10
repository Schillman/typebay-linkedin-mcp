import hashlib

import pytest

from typebay_linkedin_mcp import safety
from typebay_linkedin_mcp.safety import (
    ValidationError,
    confirm_token,
    lint_text,
    validate_images,
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
