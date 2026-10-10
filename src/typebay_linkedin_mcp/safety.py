"""Input validation and the preview/confirm token. Pure functions, no network."""

import hashlib
import json
import re
import struct
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from .config import MAX_IMAGE_BYTES, MAX_IMAGES, MAX_TEXT, MAX_VIDEO_BYTES, VISIBILITIES, allowed_image_dirs

FOLD_DESKTOP = 210
FOLD_MOBILE = 140
MAX_PARAGRAPH = 400
MAX_HASHTAGS = 5

_MAGIC = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
}


class ValidationError(ToolError):
    """Raised with a message that is safe to show the user."""


@dataclass(frozen=True)
class Image:
    path: Path
    mime: str
    sha256: str
    size: int
    alt: str
    stripped: bool = False  # derived (metadata found); deliberately not in confirm_token


@dataclass(frozen=True)
class Video:
    path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class Link:
    url: str
    title: str
    description: str


def validate_link(url: str | None, title: str | None = None, description: str | None = None) -> Link | None:
    url, title, description = (url or "").strip(), (title or "").strip(), (description or "").strip()
    if not url:
        if title or description:
            raise ValidationError("link_title and link_description need a link_url.")
        return None
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValidationError("link_url must be an http or https URL.")
    return Link(url, title, description)


def validate_text(text: str) -> str:
    text = text.strip()
    if not text:
        raise ValidationError("Post text is empty.")
    if len(text) > MAX_TEXT:
        raise ValidationError(f"Post text is {len(text)} characters, limit is {MAX_TEXT}.")
    return text


def lint_text(text: str) -> list[str]:
    """Advisory warnings about reach, never errors. A paragraph is a single line (split on newlines)."""
    out = []
    first = text.find("\n")
    if first > FOLD_DESKTOP or (first == -1 and len(text) > FOLD_DESKTOP):
        n = first if first != -1 else len(text)
        out.append(
            f"First line break comes after {n} characters; LinkedIn folds at about "
            f"{FOLD_DESKTOP} (desktop) and {FOLD_MOBILE} (mobile). Put the hook first."
        )
    out += [
        f"Paragraph of {len(p)} characters without a line break."
        for p in text.split("\n")
        if len(p) > MAX_PARAGRAPH
    ]
    if (tags := len(re.findall(r"#\w+", text))) > MAX_HASHTAGS:
        out.append(f"{tags} hashtags; 3 to 5 is the usual advice.")
    if re.search(r"https?://", text):
        out.append("URL in the body; consider link_url (preview card) or the first comment.")
    return out


def validate_visibility(visibility: str) -> str:
    if visibility not in VISIBILITIES:
        raise ValidationError(f"visibility must be one of {', '.join(VISIBILITIES)}.")
    return visibility


_JPEG_DROP = {0xE1, 0xED, 0xFE}  # APP1 (Exif/XMP), APP13 (IPTC), COM
_PNG_DROP = {b"eXIf", b"tEXt", b"iTXt", b"zTXt"}


def _strip_jpeg(data: bytes) -> bytes:
    out, i = [data[:2]], 2
    while i < len(data):
        if data[i] != 0xFF or i + 4 > len(data):
            return data
        marker = data[i + 1]
        if marker == 0xDA:  # SOS: entropy coded data and EOI follow verbatim
            out.append(data[i:])
            return b"".join(out)
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        end = i + 2 + length
        if length < 2 or end > len(data):
            return data
        if marker not in _JPEG_DROP:
            out.append(data[i:end])
        i = end
    return data  # no SOS found: malformed


def _strip_png(data: bytes) -> bytes:
    out, i = [data[:8]], 8
    while i < len(data):
        if i + 8 > len(data):
            return data
        (length,) = struct.unpack(">I", data[i : i + 4])
        end = i + 12 + length  # length + type + data + CRC
        if end > len(data):
            return data
        if data[i + 4 : i + 8] not in _PNG_DROP:
            out.append(data[i:end])
        i = end
    return b"".join(out)


def strip_metadata(data: bytes, mime: str) -> bytes:
    """Drop EXIF/XMP/IPTC/text metadata losslessly. Malformed input and GIF come back unchanged."""
    if mime == "image/jpeg":
        return _strip_jpeg(data)
    if mime == "image/png":
        return _strip_png(data)
    return data


def _inside(path: Path, roots: list[str]) -> bool:
    return any(path.is_relative_to(Path(r).resolve()) for r in roots)


def _checked_file(raw: str, max_bytes: int) -> tuple[Path, int]:
    """Shared by images and video: resolve symlinks, then allowlist, is_file and size cap."""
    path = Path(raw).expanduser().resolve()  # resolves symlinks before the check
    if not _inside(path, allowed_image_dirs()):
        raise ValidationError(
            f"{raw} is outside the allowed image directories. Set LINKEDIN_MCP_IMAGE_DIRS to allow it."
        )
    if not path.is_file():
        raise ValidationError(f"{raw} is not a file.")
    size = path.stat().st_size
    if size > max_bytes:
        raise ValidationError(f"{raw} is {size} bytes, limit is {max_bytes}.")
    return path, size


def validate_images(paths: list[str], alt_texts: list[str] | None = None) -> list[Image]:
    if len(paths) > MAX_IMAGES:
        raise ValidationError(f"At most {MAX_IMAGES} images are allowed.")
    alts = list(alt_texts or [])
    if alts and len(alts) != len(paths):
        raise ValidationError("alt_texts must have one entry per image.")
    images = []
    for i, raw in enumerate(paths):
        path, size = _checked_file(raw, MAX_IMAGE_BYTES)
        data = path.read_bytes()
        mime = next((m for sig, m in _MAGIC.items() if data.startswith(sig)), None)
        if mime is None:
            raise ValidationError(f"{raw} is not a PNG, JPEG or GIF.")
        stripped = strip_metadata(data, mime) != data
        images.append(
            Image(path, mime, hashlib.sha256(data).hexdigest(), size, alts[i] if alts else "", stripped)
        )
    return images


def validate_video(path: str | None) -> Video | None:
    raw = (path or "").strip()
    if not raw:
        return None
    path_, size = _checked_file(raw, MAX_VIDEO_BYTES)
    with path_.open("rb") as f:
        if f.read(8)[4:8] != b"ftyp":  # ISO base media file (MP4), by magic bytes not extension
            raise ValidationError(f"{raw} is not an MP4 video.")
        f.seek(0)
        digest = hashlib.file_digest(f, "sha256").hexdigest()  # streams, no full read into memory
    return Video(path_, digest, size)


_MENTION = re.compile(r"@\[([^\]]*)\]\((urn:li:[A-Za-z]+:[^)\s]*)\)")
_MENTION_KEYS = {
    "organization": ("com.linkedin.common.CompanyAttributedEntity", "company"),
    "person": ("com.linkedin.common.MemberAttributedEntity", "member"),
}


def parse_mentions(text: str) -> tuple[str, list[dict], list[dict]]:
    """Replace @[Name](urn:li:organization:1) with Name. Returns plain text, UGC attributes, mentions."""
    out, attrs, mentions, pos = [], [], [], 0
    plain_len = 0
    for m in _MENTION.finditer(text):
        name, urn = m.group(1).strip(), m.group(2)
        kind = urn.split(":")[2]
        if kind not in _MENTION_KEYS or not urn.split(":", 3)[3]:
            raise ValidationError("Mentions support urn:li:organization and urn:li:person only.")
        if not name:
            raise ValidationError("A mention needs a display name: @[Name](urn:li:organization:123).")
        before = text[pos : m.start()]
        out += [before, name]
        plain_len += len(before)
        entity, field = _MENTION_KEYS[kind]
        # ponytail: offsets are code points. LinkedIn may count UTF-16 units
        # when emoji precede a mention, upgrade path: encode to utf-16 and halve.
        attrs.append({"start": plain_len, "length": len(name), "value": {entity: {field: urn}}})
        mentions.append({"name": name, "urn": urn})
        plain_len += len(name)
        pos = m.end()
    out.append(text[pos:])
    return "".join(out), attrs, mentions


def confirm_token(
    text: str,
    images: list[Image],
    visibility: str,
    link: Link | None = None,
    video: Video | None = None,
    reshare_of: str | None = None,
) -> str:
    """Binds a publish call to exactly the content that was previewed."""
    key = {
        "t": text,
        "i": [(im.sha256, im.alt) for im in images],
        "v": visibility,
        "l": asdict(link) if link else None,
        "vid": video.sha256 if video else None,
    }
    if reshare_of:  # only added when set, so tokens for plain posts are unchanged
        key["r"] = reshare_of
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def comment_token(post_urn: str, text: str) -> str:
    """Binds add_comment to the previewed comment text and target post."""
    payload = json.dumps({"comment_on": post_urn, "t": text}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
