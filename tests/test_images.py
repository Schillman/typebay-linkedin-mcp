import struct
import zlib

import httpx
import respx

from typebay_linkedin_mcp.api import LinkedInClient
from typebay_linkedin_mcp.safety import strip_metadata, validate_images

GPS = b"GPS-LAT-59.33N"


def seg(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def jpeg() -> bytes:
    return (
        b"\xff\xd8"
        + seg(0xE0, b"JFIF\x00")
        + seg(0xE1, b"Exif\x00\x00" + GPS)
        + seg(0xFE, b"a comment")
        + seg(0xDB, b"\x00" + b"\x10" * 64)
        + seg(0xDA, b"\x00")
        + b"\x01\x02\x03"
        + b"\xff\xd9"
    )


def chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


SIG = b"\x89PNG\r\n\x1a\n"
IHDR = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
IDAT = chunk(b"IDAT", b"\x00\x01\x02")
IEND = chunk(b"IEND", b"")


def png() -> bytes:
    return SIG + IHDR + chunk(b"tEXt", b"GPS\x00" + GPS) + chunk(b"eXIf", GPS) + IDAT + IEND


def test_jpeg_strip():
    out = strip_metadata(jpeg(), "image/jpeg")
    assert GPS not in out and b"a comment" not in out
    assert b"JFIF" in out and b"\xff\xdb" in out
    assert out.startswith(b"\xff\xd8") and out.endswith(b"\x01\x02\x03\xff\xd9")
    assert out == jpeg().replace(seg(0xE1, b"Exif\x00\x00" + GPS), b"").replace(seg(0xFE, b"a comment"), b"")


def test_png_strip():
    assert strip_metadata(png(), "image/png") == SIG + IHDR + IDAT + IEND


def test_malformed_and_gif_unchanged():
    bad = jpeg()[:20]  # cuts inside the APP1 segment
    assert strip_metadata(bad, "image/jpeg") == bad
    assert strip_metadata(png()[:-5], "image/png") == png()[:-5]
    gif = b"GIF89a" + GPS
    assert strip_metadata(gif, "image/gif") == gif


@respx.mock
def test_upload_body_has_no_gps(tmp_path, monkeypatch):
    monkeypatch.setenv("LINKEDIN_MCP_IMAGE_DIRS", str(tmp_path))
    f = tmp_path / "a.jpg"
    f.write_bytes(jpeg())
    images = validate_images([str(f)])
    assert images[0].stripped
    key = "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"
    respx.post("https://api.linkedin.com/v2/assets?action=registerUpload").respond(
        200,
        json={"value": {"uploadMechanism": {key: {"uploadUrl": "https://upload.example/u"}}, "asset": "A"}},
    )
    put = respx.put("https://upload.example/u").respond(201)
    client = LinkedInClient("T", "urn:li:person:x", httpx.Client(base_url="https://api.linkedin.com"))
    assert client.upload_image(images[0]) == "A"
    body = put.calls.last.request.read()
    assert GPS not in body and body.startswith(b"\xff\xd8")
