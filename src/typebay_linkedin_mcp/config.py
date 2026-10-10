"""Constants and environment overrides. No secrets live here."""

import os

SERVICE = "typebay-linkedin-mcp"
AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
# Public OAuth endpoint URL, not a credential.
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"  # noqa: S105
API_BASE = "https://api.linkedin.com"
INTROSPECT_URL = "https://www.linkedin.com/oauth/v2/introspectToken"
SCOPES = "openid profile w_member_social"
DEFAULT_PORT = 8976

MAX_TEXT = 3000
MAX_IMAGES = 20
MAX_IMAGE_BYTES = int(os.environ.get("LINKEDIN_MCP_MAX_IMAGE_BYTES", 10 * 1024 * 1024))
MAX_VIDEO_BYTES = int(os.environ.get("LINKEDIN_MCP_MAX_VIDEO_BYTES", 200 * 1024 * 1024))
VISIBILITIES = ("PUBLIC", "CONNECTIONS")  # the two values documented for Share on LinkedIn
CONFIRM_TTL_SECONDS = 15 * 60


def allowed_image_dirs() -> list[str]:
    """Directories images may be read from. Default: the server's working directory."""
    raw = os.environ.get("LINKEDIN_MCP_IMAGE_DIRS")
    return raw.split(os.pathsep) if raw else [os.getcwd()]
