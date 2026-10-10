"""Thin LinkedIn REST client for the self-serve Share on LinkedIn product (UGC Post API).

Wire format per Microsoft Learn: linkedin/consumer/integrations/self-serve/share-on-linkedin.
"""

import logging
import time
import urllib.parse

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from .config import API_BASE
from .safety import Image, Link, Video, strip_metadata

log = logging.getLogger(__name__)

_UPLOAD_KEY = "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"
POLL_INTERVAL = 5
POLL_DEADLINE = 120


class LinkedInError(ToolError):
    """Raised with a message that is safe to show the user. Never contains tokens."""


def error_message(resp: httpx.Response) -> str:
    """The `message` field of a JSON error body, or "". Never falls back to raw text."""
    try:
        msg = resp.json().get("message")
    except (ValueError, AttributeError):  # non JSON body, or JSON that is not an object
        return ""
    return msg if isinstance(msg, str) else ""


def _explain(resp: httpx.Response) -> str:
    log.debug("LinkedIn error %s: %s", resp.status_code, resp.text[:500])  # raw body: log only
    status, msg = resp.status_code, error_message(resp)
    if status == 401:
        return "LinkedIn rejected the token (expired or revoked). Run: typebay-linkedin-mcp login"
    if status == 422 and "duplicate" in msg.lower():
        return "LinkedIn rejected the post as a duplicate of one published in the last 10 minutes."
    if status == 429:
        if "throttl" in msg.lower():
            return "LinkedIn is throttling comments (1 per minute). Wait a minute and retry."
        return "LinkedIn rate limit hit (150 requests per member per day). Try again later."
    if status == 403:
        return "LinkedIn denied the request. Check the app has the Share on LinkedIn product."
    if status == 410:
        return (
            "LinkedIn retired this endpoint (the UGC Post API is labelled Legacy). "
            "Check for a newer version of typebay-linkedin-mcp."
        )
    return f"LinkedIn API error {status}: {msg}" if msg else f"LinkedIn API error {status}."


class LinkedInClient:
    def __init__(self, access_token: str, author_urn: str, http: httpx.Client | None = None):
        self.author = author_urn
        self._http = http or httpx.Client(base_url=API_BASE, timeout=30)
        self._auth = {"Authorization": f"Bearer {access_token}"}
        self._headers = {**self._auth, "X-Restli-Protocol-Version": "2.0.0"}

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        resp = self._http.request(method, url, headers=self._headers, **kw)
        if resp.status_code >= 400:
            raise LinkedInError(_explain(resp))
        return resp

    def _register_upload(self, recipe: str) -> dict:
        return self._request(
            "POST",
            "/v2/assets?action=registerUpload",
            json={
                "registerUploadRequest": {
                    "recipes": [recipe],
                    "owner": self.author,
                    "serviceRelationships": [
                        {"relationshipType": "OWNER", "identifier": "urn:li:userGeneratedContent"}
                    ],
                }
            },
        ).json()["value"]

    def upload_image(self, image: Image) -> str:
        """Register, then upload. Returns the digitalmediaAsset URN."""
        value = self._register_upload("urn:li:digitalmediaRecipe:feedshare-image")
        upload_url = value["uploadMechanism"][_UPLOAD_KEY]["uploadUrl"]
        put = self._http.put(
            upload_url, content=strip_metadata(image.path.read_bytes(), image.mime), headers=self._auth
        )
        if put.status_code >= 400:
            raise LinkedInError(f"Image upload failed ({put.status_code}).")
        return value["asset"]

    def upload_video(self, video: Video) -> str:
        """Register, stream the file, then poll until the asset is AVAILABLE. Returns the asset URN."""
        value = self._register_upload("urn:li:digitalmediaRecipe:feedshare-video")
        upload_url = value["uploadMechanism"][_UPLOAD_KEY]["uploadUrl"]
        with video.path.open("rb") as f:
            put = self._http.put(upload_url, content=f, headers=self._auth)
        if put.status_code >= 400:
            raise LinkedInError(f"Video upload failed ({put.status_code}).")
        asset = value["asset"]
        waited = 0
        while True:  # polling the GET is idempotent, unlike the post
            recipes = self._request("GET", f"/v2/assets/{asset.rsplit(':', 1)[-1]}").json().get("recipes", [])
            statuses = [r.get("status") for r in recipes]
            if {"CLIENT_ERROR", "SERVER_ERROR"} & set(statuses):
                raise LinkedInError("LinkedIn could not process the video. Check it is a valid MP4.")
            if statuses and all(s == "AVAILABLE" for s in statuses):
                return asset
            if waited >= POLL_DEADLINE:
                raise LinkedInError(
                    f"LinkedIn is still processing the video after {POLL_DEADLINE} seconds. Try again later."
                )
            time.sleep(POLL_INTERVAL)
            waited += POLL_INTERVAL

    def create_post(
        self,
        text: str,
        visibility: str,
        images: list[Image],
        link: Link | None = None,
        video: Video | None = None,
    ) -> str:
        content: dict = {
            "shareCommentary": {"text": text},
            "shareMediaCategory": "IMAGE" if images else "NONE",
        }
        if images:
            content["media"] = [
                {
                    "status": "READY",
                    "description": {"text": im.alt},
                    "media": self.upload_image(im),
                    "title": {"text": im.alt or im.path.stem},
                }
                for im in images
            ]
        if video:
            content["shareMediaCategory"] = "VIDEO"
            content["media"] = [
                {"status": "READY", "media": self.upload_video(video), "title": {"text": video.path.stem}}
            ]
        if link:
            content["shareMediaCategory"] = "ARTICLE"
            media = {"status": "READY", "originalUrl": link.url}
            if link.title:
                media["title"] = {"text": link.title}
            if link.description:
                media["description"] = {"text": link.description}
            content["media"] = [media]
        body = {
            "author": self.author,
            "lifecycleState": "PUBLISHED",
            "specificContent": {"com.linkedin.ugc.ShareContent": content},
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": visibility},
        }
        return self._request("POST", "/v2/ugcPosts", json=body).headers["x-restli-id"]

    def add_comment(self, post_urn: str, text: str) -> None:
        quoted = urllib.parse.quote(post_urn, safe="")
        self._request(
            "POST",
            f"/v2/socialActions/{quoted}/comments",
            json={"actor": self.author, "message": {"text": text}},
        )

    def delete_post(self, post_urn: str) -> None:
        self._request("DELETE", f"/v2/ugcPosts/{urllib.parse.quote(post_urn, safe='')}")


def post_url(urn: str) -> str:
    return f"https://www.linkedin.com/feed/update/{urn}/"
