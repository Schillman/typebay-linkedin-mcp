"""MCP tool surface. Tools are plain functions so tests can call them directly."""

import logging
import os
import time
from typing import NotRequired, TypedDict

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import postlog, store
from .api import LinkedInClient, LinkedInError, LinkedInUnknownOutcome, post_url
from .auth import introspect
from .config import CONFIRM_TTL_SECONDS
from .safety import (
    FOLD_DESKTOP,
    ValidationError,
    lint_text,
    validate_images,
    validate_link,
    validate_text,
    validate_video,
    validate_visibility,
)
from .safety import confirm_token as make_confirm_token

mcp = MCPServer(
    "typebay-linkedin",
    instructions=(
        "Publishes posts to the user's own LinkedIn profile. Always call "
        "linkedin_preview_post first, show the user the exact text and images, and only call "
        "linkedin_publish_post after the user explicitly approves, passing the confirm_token."
    ),
)


class StatusResult(TypedDict):
    logged_in: bool
    fix: NotRequired[str]
    name: NotRequired[str | None]
    status: NotRequired[str | None]
    scope: NotRequired[str | None]
    source: NotRequired[str]
    days_left: NotRequired[int]
    renew: NotRequired[str]


class LinkInfo(TypedDict):
    url: str
    title: str | None
    description: str | None


class PostRecord(TypedDict):
    urn: str
    url: NotRequired[str]
    text: NotRequired[str]
    visibility: NotRequired[str]
    images: NotRequired[list[str]]
    video: NotRequired[str | None]
    link: NotRequired[LinkInfo | None]
    ts: NotRequired[int]


class ListResult(TypedDict):
    posts: list[PostRecord]
    source: str


class PreviewResult(TypedDict):
    preview: str
    characters: int
    above_fold: str
    visibility: str
    images: list[str]
    confirm_token: str
    next: str
    link: NotRequired[LinkInfo]
    video: NotRequired[str]
    metadata_stripped: NotRequired[list[str]]
    warnings: NotRequired[list[str]]


class PublishResult(TypedDict):
    published: bool
    urn: str
    url: str


class CommentResult(TypedDict):
    commented: bool
    url: str
    note: NotRequired[str]


class DeleteResult(TypedDict):
    deleted: bool
    urn: str


log = logging.getLogger(__name__)

READ_ONLY = os.environ.get("LINKEDIN_MCP_READ_ONLY", "").lower() in ("1", "true", "yes")


def _write_tool(annotations: ToolAnnotations):
    """Like mcp.tool, but the tool is not registered at all in read-only mode."""
    return (lambda fn: fn) if READ_ONLY else mcp.tool(annotations=annotations)


_previews: dict[str, float] = {}  # confirm_token -> issued_at


def _client() -> LinkedInClient:
    token = store.load_token()
    if not token:
        raise LinkedInError("Not logged in. Run: typebay-linkedin-mcp login")
    if token["expires_at"] <= time.time():
        raise LinkedInError("Token expired. Run: typebay-linkedin-mcp login")
    return LinkedInClient(token["access_token"], token["author_urn"])


@mcp.tool(
    annotations=ToolAnnotations(title="Check LinkedIn login", read_only_hint=True, open_world_hint=False)
)
def linkedin_status() -> StatusResult:
    """Report whether the user is logged in to LinkedIn and how many days the token has left."""
    token = store.load_token()
    if not token:
        return {"logged_in": False, "fix": "Run: typebay-linkedin-mcp login"}
    cid, secret = store.get_client("client_id"), store.get_client("client_secret")
    info = introspect(token["access_token"], cid, secret) if cid and secret else None
    if info:
        out = {
            "logged_in": info.get("status") == "active",
            "name": token.get("name"),
            "status": info.get("status"),
            "scope": info.get("scope"),
            "source": "linkedin",
        }
        expires_at = info.get("expires_at", token["expires_at"])
    else:
        expires_at = token["expires_at"]
        out = {"logged_in": expires_at > time.time(), "name": token.get("name"), "source": "local"}
    out["days_left"] = int((expires_at - time.time()) // 86400)
    if out["days_left"] < 14:
        out["renew"] = "Run: typebay-linkedin-mcp login. Done before expiry it skips the consent screen."
    return out


def _log_record(entry: dict) -> None:
    """The action already happened, so a log failure must not fail the tool."""
    try:
        postlog.record(entry)
    except OSError as e:
        log.warning("Could not write post log: %s", e)


NOT_LOGGED = "URN not in the local post log; it was not published by this server."


@mcp.tool(
    annotations=ToolAnnotations(
        title="List posts published by this server", read_only_hint=True, open_world_hint=False
    )
)
def linkedin_list_my_posts(limit: int = 20) -> ListResult:
    """List posts this server published (newest first, deleted ones excluded), from a local log."""
    return {"posts": postlog.posts(max(1, min(limit, 100))), "source": str(postlog.path())}


def _validate(text, image_paths, alt_texts, visibility, link_url, link_title, link_description, video_path):
    """Shared by preview and publish so both hash exactly the same validated content."""
    text = validate_text(text)
    visibility = validate_visibility(visibility)
    images = validate_images(image_paths or [], alt_texts)
    link = validate_link(link_url, link_title, link_description)
    video = validate_video(video_path)
    if sum(map(bool, (images, video, link))) > 1:
        raise ValidationError("A post can have images, a video or a link, only one of them.")
    return text, visibility, images, link, video


@mcp.tool(
    annotations=ToolAnnotations(title="Preview LinkedIn post", read_only_hint=True, open_world_hint=False)
)
def linkedin_preview_post(
    text: str,
    image_paths: list[str] | None = None,
    alt_texts: list[str] | None = None,
    visibility: str = "PUBLIC",
    link_url: str | None = None,
    link_title: str | None = None,
    link_description: str | None = None,
    video_path: str | None = None,
) -> PreviewResult:
    """Validate a post and return the exact preview plus a confirm_token. Does not publish."""
    text, visibility, images, link, video = _validate(
        text, image_paths, alt_texts, visibility, link_url, link_title, link_description, video_path
    )
    token = make_confirm_token(text, images, visibility, link, video)
    now = time.time()
    for t in [t for t, issued in _previews.items() if now - issued > CONFIRM_TTL_SECONDS]:
        del _previews[t]
    _previews[token] = now
    out: PreviewResult = {
        "preview": text,
        "characters": len(text),
        "above_fold": text[:FOLD_DESKTOP],
        "visibility": visibility,
        "images": [str(i.path) for i in images],
        "confirm_token": token,
        "next": "Show this to the user. Publish only after explicit approval.",
    }
    if link:
        out["link"] = {"url": link.url, "title": link.title, "description": link.description}
    if video:
        out["video"] = str(video.path)
    if stripped := [str(i.path) for i in images if i.stripped]:
        out["metadata_stripped"] = stripped
    if warnings := lint_text(text):
        out["warnings"] = warnings
    return out


@_write_tool(
    annotations=ToolAnnotations(
        title="Publish LinkedIn post", destructive_hint=False, idempotent_hint=False, open_world_hint=True
    )
)
def linkedin_publish_post(
    text: str,
    confirm_token: str,
    image_paths: list[str] | None = None,
    alt_texts: list[str] | None = None,
    visibility: str = "PUBLIC",
    link_url: str | None = None,
    link_title: str | None = None,
    link_description: str | None = None,
    video_path: str | None = None,
) -> PublishResult:
    """Publish a previewed post. Fails unless content matches a recent preview exactly."""
    text, visibility, images, link, video = _validate(
        text, image_paths, alt_texts, visibility, link_url, link_title, link_description, video_path
    )
    expected = make_confirm_token(text, images, visibility, link, video)
    issued = _previews.get(expected)
    if confirm_token != expected or issued is None or time.time() - issued > CONFIRM_TTL_SECONDS:
        raise ValidationError("Content does not match a recent preview. Call linkedin_preview_post again.")
    try:
        urn = _client().create_post(text, visibility, images, link, video)
    except LinkedInUnknownOutcome:
        _previews.pop(expected, None)  # never retry a publish that may have landed
        _log_record({"unknown_outcome": True, "text": text, "visibility": visibility, "ts": int(time.time())})
        raise
    _previews.pop(expected, None)  # one publish per preview
    _log_record(
        {
            "urn": urn,
            "url": post_url(urn),
            "text": text,
            "visibility": visibility,
            "images": [str(i.path) for i in images],
            "video": str(video.path) if video else None,
            "link": {"url": link.url, "title": link.title, "description": link.description} if link else None,
            "ts": int(time.time()),
        }
    )
    return {"published": True, "urn": urn, "url": post_url(urn)}


@_write_tool(
    annotations=ToolAnnotations(
        title="Comment on LinkedIn post", destructive_hint=False, idempotent_hint=False, open_world_hint=True
    )
)
def linkedin_add_comment(post_urn: str, text: str) -> CommentResult:
    """Add a comment (for example a link) to one of the user's posts. Show the text to the user first."""
    _client().add_comment(post_urn, validate_text(text))
    out = {"commented": True, "url": post_url(post_urn)}
    if not postlog.known(post_urn):
        out["note"] = NOT_LOGGED
    return out


@_write_tool(
    annotations=ToolAnnotations(
        title="Delete LinkedIn post", destructive_hint=True, idempotent_hint=True, open_world_hint=True
    )
)
def linkedin_delete_post(post_urn: str, confirm: bool = False) -> DeleteResult:
    """Permanently delete one of the user's posts. Ask the user first, then call again with confirm=true."""
    if not confirm:
        msg = f"Deleting {post_urn} is permanent. Ask the user, then pass confirm=true."
        raise ValidationError(msg if postlog.known(post_urn) else f"{msg} {NOT_LOGGED}")
    _client().delete_post(post_urn)
    _log_record({"deleted": post_urn, "ts": int(time.time())})
    return {"deleted": True, "urn": post_urn}


_WORKFLOW = (
    "First call linkedin_list_my_posts and match my voice from recent posts, if any.\n"
    "Write the post about {topic}: hook in the first 210 characters, short paragraphs, at most 5 hashtags.\n"
    "Then call linkedin_preview_post and show me the preview and any warnings.\n"
    "Never call linkedin_publish_post without my explicit approval."
)


# Prompts only produce text, so they are registered even in read-only mode.
@mcp.prompt()
def announcement(topic: str) -> str:
    """Draft an announcement post and stop at the preview."""
    return "Draft a LinkedIn announcement.\n" + _WORKFLOW.format(topic=topic)


@mcp.prompt()
def lessons_learned(topic: str) -> str:
    """Draft a lessons learned post and stop at the preview."""
    return "Draft a LinkedIn lessons learned post.\n" + _WORKFLOW.format(topic=topic)


@mcp.prompt()
def link_with_first_comment(url: str, topic: str) -> str:
    """Draft a post that shares a link, with the link in the post or in a first comment."""
    return (
        f"Draft a LinkedIn post sharing {url}.\n"
        + _WORKFLOW.format(topic=topic)
        + f"\nPut the link in link_url with a title and description. If I prefer a link free body, "
        f"keep the text link free and plan a linkedin_add_comment with {url} after publish."
    )
