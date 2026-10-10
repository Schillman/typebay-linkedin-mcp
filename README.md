# typebay-linkedin-mcp

Publish to your own LinkedIn profile from Claude, with a preview you approve before anything goes out.

[![ci](https://github.com/Schillman/typebay-linkedin-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Schillman/typebay-linkedin-mcp/actions/workflows/ci.yml)
[![codeql](https://github.com/Schillman/typebay-linkedin-mcp/actions/workflows/codeql.yml/badge.svg)](https://github.com/Schillman/typebay-linkedin-mcp/actions/workflows/codeql.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

An MCP server for LinkedIn's official self-serve API (Share on LinkedIn, UGC Post API). Text, images, link shares, a first comment, delete. Bring your own LinkedIn app: nothing secret ships in this package, and credentials never enter a chat transcript.

## Quick start

You need a LinkedIn app first (two minutes, see [Setup](#setup)). Then:

    uvx --from git+https://github.com/Schillman/typebay-linkedin-mcp typebay-linkedin-mcp login
    claude mcp add linkedin --scope user -- uvx --from git+https://github.com/Schillman/typebay-linkedin-mcp typebay-linkedin-mcp

Ask Claude to preview a post. You get the exact text, a `confirm_token`, and a few advisory warnings. Publishing only happens after you approve the `linkedin_publish_post` call in your client's permission prompt.

## How a post gets out

1. `linkedin_preview_post` validates the post and returns the exact text plus a `confirm_token` bound to the content.
2. Your MCP client asks you to approve `linkedin_publish_post`. That prompt is the consent step, so do not auto-allow it.
3. `linkedin_publish_post` recomputes the token from what it was given. Anything changed since the preview, or older than 15 minutes, is refused.

## Features

- **Text posts** up to 3000 characters, visibility `PUBLIC` or `CONNECTIONS`.
- **Image posts**: one or more PNG/JPEG/GIF files, with alt text per image.
- **Link posts**: a URL with a preview card, optional title and description.
- **First comment** on your own post, for example to keep a link out of the body.
- **Delete** your own post, only with an explicit `confirm=true`.
- **Status**: login state, token status and scope straight from LinkedIn, days until expiry, and a renew nudge.
- **Post lint** in every preview: fold position, paragraph length, hashtag count, links in the body.
- **Local post log and `linkedin_list_my_posts`**: LinkedIn offers no read back scope, so the server logs what it publishes and lists it from there.
- **Metadata stripping**: EXIF, XMP, IPTC and text metadata (including GPS) are removed from JPEG and PNG before upload.
- **Prompt templates**: three prompts that draft a post and stop at the preview.
- **`doctor`**: checks credentials, token, scopes and callback port, with a fix hint per failure.
- **Read only mode** (`LINKEDIN_MCP_READ_ONLY=1`) leaves only status, preview and list_my_posts registered.

## Safety model

- **Preview, then publish.** The token proves *what* is published matches what was previewed. It is not human consent. **Human approval comes from your MCP client's permission prompt, so do not auto-allow `linkedin_publish_post`, `linkedin_add_comment` or `linkedin_delete_post`.**
- Credentials are typed into a hidden CLI prompt and stored in the OS keychain. They are never tool arguments, never logged, never in error messages.
- Images must be PNG/JPEG/GIF (magic bytes checked), under 10 MB, and inside an allowed directory (default: the server's working directory; set `LINKEDIN_MCP_IMAGE_DIRS`). Symlinks are resolved before the check.
- EXIF, XMP, IPTC and text metadata (including GPS) are stripped from JPEG and PNG before upload; pixel data and colour profiles are untouched.
- Minimal scopes: `openid profile w_member_social`. Personal profile only. Visibility is `PUBLIC` or `CONNECTIONS`.
- Never retries a publish. 401 and 429 give a plain message. The post log holds text and URNs only.

## Setup

1. Create an app at <https://www.linkedin.com/developers/apps>. On the **Products** tab add **Share on LinkedIn** and **Sign In with LinkedIn using OpenID Connect**. Use a dedicated app: the Community Management API must be the only product on its app, and an app that has it cannot add these two.
2. On the **Auth** tab add the redirect URL `http://localhost:8976/callback`.
3. Log in. This prompts for Client ID and Secret (hidden), opens the browser, and stores everything in your keychain:

       uvx --from git+https://github.com/Schillman/typebay-linkedin-mcp typebay-linkedin-mcp login

4. Register the server with your client (Claude Code shown in the quick start, others below).

Access tokens last about 60 days. When `linkedin_status` shows few days left, run `login` again. Programmatic refresh is documented for approved Marketing partners only, so self serve apps cannot refresh.

**Troubleshooting `unauthorized_scope_error` for `openid`:** the app lacks the Sign In with LinkedIn using OpenID Connect product. Share on LinkedIn alone grants only `w_member_social`, and LinkedIn documents no way to read your member ID without `openid`/`profile`. Add the product on the Products tab, or create a fresh app and add both products there.

### Other clients

No credentials go in these files. Run `login` once first.

<details>
<summary>Claude Desktop (<code>claude_desktop_config.json</code>)</summary>

    {
      "mcpServers": {
        "linkedin": {
          "command": "uvx",
          "args": ["--from", "git+https://github.com/Schillman/typebay-linkedin-mcp", "typebay-linkedin-mcp"]
        }
      }
    }

</details>

<details>
<summary>VS Code (<code>.vscode/mcp.json</code>)</summary>

    {
      "servers": {
        "linkedin": {
          "command": "uvx",
          "args": ["--from", "git+https://github.com/Schillman/typebay-linkedin-mcp", "typebay-linkedin-mcp"]
        }
      }
    }

</details>

<details>
<summary>Claude Code, project scope (<code>.mcp.json</code>)</summary>

Same shape as Claude Desktop. A ready made copy is in `.mcp.json.example`.

</details>

## Tools

The permission prompt of your MCP client is the consent gate for the three write tools, so do not auto-allow them.

| Tool | What it does | Parameters (name: type, default) | Annotations | Needs human approval |
|---|---|---|---|---|
| `linkedin_status` | Reports login state and days left on the token. | none | read only, not open world | no |
| `linkedin_preview_post` | Validates a post, returns the exact preview, a `confirm_token`, `above_fold` and advisory `warnings`. Does not publish. | `text: str` (required), `image_paths: list[str] \| null = null`, `alt_texts: list[str] \| null = null`, `visibility: str = "PUBLIC"`, `link_url: str \| null = null`, `link_title: str \| null = null`, `link_description: str \| null = null` | read only, not open world | no |
| `linkedin_publish_post` | Publishes a previewed post. Fails unless the content matches a preview under 15 minutes old. | `text: str` (required), `confirm_token: str` (required), `image_paths: list[str] \| null = null`, `alt_texts: list[str] \| null = null`, `visibility: str = "PUBLIC"`, `link_url: str \| null = null`, `link_title: str \| null = null`, `link_description: str \| null = null` | not destructive, not idempotent, open world | yes |
| `linkedin_list_my_posts` | Lists posts this server published (newest first, deleted excluded) from a local log. | `limit: int = 20` (clamped 1 to 100) | read only, not open world | no |
| `linkedin_add_comment` | Adds a comment (for example a link) to one of your posts. | `post_urn: str` (required), `text: str` (required) | not destructive, not idempotent, open world | yes |
| `linkedin_delete_post` | Permanently deletes one of your posts. Refuses unless `confirm=true`. | `post_urn: str` (required), `confirm: bool = false` | destructive, idempotent, open world | yes |

Every tool returns a typed result (`outputSchema`), so clients can rely on fields such as `confirm_token`, `urn` and `url`.

A post can carry images or a link with a preview card, not both. Title and description are optional, and LinkedIn does not scrape them from the URL.

## Prompts

Clients that list MCP prompts show these (Claude Code `/mcp` prompts, Claude Desktop, VS Code). All three stop at the preview and never publish.

- `announcement(topic)`: drafts an announcement post.
- `lessons_learned(topic)`: drafts a lessons learned post.
- `link_with_first_comment(url, topic)`: drafts a link post, with the link in the card or in a first comment.

## Commands

`typebay-linkedin-mcp [serve|login|logout|status|doctor] [--port 8976] [--version]`

- `serve` runs the MCP server on stdio (what the client config starts).
- `login` stores app credentials and a token in the keychain. `logout` removes them.
- `status` prints login state and days left. `doctor` runs every check with a fix hint per failure.

## Configuration

| Variable | Effect |
|---|---|
| `LINKEDIN_MCP_READ_ONLY=1` | Registers only `linkedin_status`, `linkedin_preview_post` and `linkedin_list_my_posts`, plus the prompts. |
| `LINKEDIN_MCP_STATE_DIR` | Post log directory. Default `$XDG_STATE_HOME/typebay-linkedin-mcp`. |
| `LINKEDIN_MCP_IMAGE_DIRS` | Allowed image directories, path separator delimited. Default: the working directory. |
| `LINKEDIN_MCP_MAX_IMAGE_BYTES` | Image size cap. Default 10 MB. |
| `LINKEDIN_MCP_LOG_LEVEL` | Default `WARNING`. `DEBUG` logs raw LinkedIn error bodies to stderr. |
| `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET` | Override the keychain, for CI. |

## Limits

- Share on LinkedIn allows 150 requests per member per day. A post with images costs one request per image plus two.
- LinkedIn's API cannot create native Articles, only posts (text, images, link shares).
- The `/rest/posts` Posts API belongs to the vetted Community Management API (registered organizations, commercial use). This server deliberately uses the self-serve UGC API instead.
- The UGC Post API page on Microsoft Learn is labelled Legacy and says the Posts API replaces it. The listed sunset applies to Marketing partners only, so this is a risk to watch, not a current break. A 410 from LinkedIn is reported as a retirement notice.
- Verified by a live test on 2026-10-10: multi image posts, first comment, delete, link share. Still unverified: how `#hashtags` render, and video. See `PLAN.md`.

## Development

    uv run pytest
    uv run ruff check src tests
    uv run ruff format --check src tests

See `CONTRIBUTING.md` for the rules around tool names and the schema snapshot, and `SECURITY.md` for reporting.

## License

MIT, see `LICENSE`.
