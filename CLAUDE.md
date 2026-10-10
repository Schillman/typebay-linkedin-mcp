# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

    uv run pytest                              # all tests
    uv run pytest tests/test_safety.py::test_name   # single test
    uv run ruff check src tests                # lint (line length 110; rules E, F, I, B, UP, SIM, S; tests ignore S101)
    uv run ruff format --check src tests
    UPDATE_TOOLSNAPS=1 uv run pytest tests/test_tools_snapshot.py   # regenerate tool schema snapshot after any tool signature, title or annotation change
    uv run typebay-linkedin-mcp [serve|login|logout|status|doctor] [--port 8976]

Python 3.12+, `uv` with `uv_build` backend. HTTP in tests is mocked with `respx`.

## Architecture

Local stdio MCP server (official `mcp` SDK, `MCPServer`) that posts to the user's own LinkedIn profile via the self-serve UGC Post API (`/v2/ugcPosts`, `/v2/assets`), not the vetted `/rest/posts` API (used only for edit, which works with `w_member_social`). Bring-your-own LinkedIn app; nothing secret ships in the package. `PLAN.md` holds the design rationale and the verified behaviors (live tests 2026-10-10, nothing left unverified).

Module flow, all under `src/typebay_linkedin_mcp/`:
- `__init__.py`: CLI entry (`main`). Imports `auth`/`server` lazily per subcommand. `serve` runs `mcp.run("stdio")`. Configures logging to stderr, level from `LINKEDIN_MCP_LOG_LEVEL`.
- `server.py`: nine tools (status, list_my_posts, preview_post, publish_post, preview_comment, add_comment, delete_post, react_to_post, edit_post) and three prompts (`announcement`, `lessons_learned`, `link_with_first_comment`; prompts stay registered in read-only mode). Tools are plain functions (tests call them directly) returning TypedDict results, which the SDK publishes as `outputSchema`. Read-only mode registers four tools (status, list, preview_post, preview_comment). Shared `_validate` for preview and publish. `linkedin_preview_comment` issues the comment token; `linkedin_add_comment` requires it. `linkedin_react_to_post` (`post_urn`, `remove=false`; LIKE only via `/v2/socialActions/{urn}/likes`, `remove=true` unlikes) and `linkedin_edit_post` (`confirm=true` gate) are write tools. `linkedin_status` calls `auth.introspect` when client creds are in the keychain, else falls back to the stored expiry (`source` field says which). Holds the in-memory `_previews` dict (confirm_token to issue time), pruned of expired entries on each new preview.
- `safety.py`: pure validation (text length, visibility, image allowlist/magic bytes/size, `Video` and `validate_video` for MP4 magic bytes and size cap, links via `Link` and `validate_link`) `confirm_token` and `comment_token` (post URN plus text), `parse_mentions` (raw `@[Name](urn)` text to plain text, UGC attributes and a mentions list), plus `lint_text` (advisory preview warnings) and `strip_metadata` (JPEG/PNG EXIF, XMP, IPTC, text chunks).
- `postlog.py`: local JSONL post log (under `LINKEDIN_MCP_STATE_DIR`) written on publish and delete; backs `linkedin_list_my_posts` and the known URN check.
- `doctor.py`: the `doctor` subcommand, checks creds, token, scopes and callback port with fix hints.
- `api.py`: `LinkedInClient`, a thin httpx wrapper; `upload_video` polls the asset status until AVAILABLE before the post. `_explain` maps 401, 403, 422 duplicate and 429 (throttle vs daily cap) to user safe messages and never echoes the raw body (logged at debug only). `like`/`unlike` use the legacy `/v2/socialActions/{urn}/likes`; `edit_post` uses `/rest/posts` with a `Linkedin-Version` header (`_rest_headers`); `/v2` calls do not. `create_post` passes `unknown_outcome=True`, so a transport error on the publish POST raises `LinkedInUnknownOutcome`.
- `auth.py`: OAuth authorization code flow for the `login` CLI (localhost callback, `state` check, code exchange, member lookup).
- `store.py`: OS keychain via `keyring`. `LINKEDIN_CLIENT_ID`/`LINKEDIN_CLIENT_SECRET` env vars override stored client creds. Keychain errors become `StoreError` (a `ToolError`, class name and fix hint only).
- `config.py`: constants and env overrides (`LINKEDIN_MCP_IMAGE_DIRS`, `LINKEDIN_MCP_MAX_IMAGE_BYTES`, `LINKEDIN_MCP_MAX_VIDEO_BYTES`, `LINKEDIN_MCP_API_VERSION` for the `Linkedin-Version` header, default 202609).

## Invariants to preserve

- **Preview then publish gate.** `linkedin_publish_post` recomputes the token from text, image content hashes, the video content hash, alt texts, link fields and visibility, and requires a matching preview under 15 minutes old. Each preview publishes once (popped only after a successful publish). The token proves content equality, not consent: human approval comes from the MCP client's permission prompt, so publish/comment/react/edit/delete must not be auto-allowed. Comments go through `linkedin_preview_comment`, the token binds post URN and text.
- **stdout is protocol only.** Log to stderr. The `status` subcommand is the only thing that prints to stdout, and it is not run under `serve`.
- **No secrets anywhere.** Credentials are never tool arguments, never logged, never in error messages. `LinkedInError` and `ValidationError` messages must be safe to show the user. Both must subclass the SDK's `ToolError`: the SDK replaces any other exception's text with "Error executing tool X", losing the message (a test covers it).
- **Never retry a publish.** Retries are only acceptable for idempotent calls. A transport failure on the publish POST is an unknown outcome: pop the preview, log it, never retry.
- **Image and video safety.** Paths are resolved (symlinks followed) before the allowlist check; type is decided by magic bytes, not extension. Images, a video, a link and a reshare are mutually exclusive.
- **Mentions:** the token binds the raw `@[Name](urn)` text; LinkedIn receives the plain text plus attributes.
- **Metadata is stripped before upload; the token binds the original file hash, not the stripped bytes.**
- **The post log holds text and URNs only, never tokens.** A logging failure must not fail a publish (`_log_record` swallows `OSError`).
- **Tool names and schemas are a contract.** Renames break clients' `mcp__linkedin__<name>` permission rules. Schema changes must regenerate the snapshot.
- `LINKEDIN_MCP_READ_ONLY=1` must keep write tools unregistered (see `_write_tool` in `server.py`), leaving four tools. `linkedin_delete_post` and `linkedin_edit_post` refuse without `confirm=true`.
- Scopes stay `openid profile w_member_social`. Visibility is only `PUBLIC` or `CONNECTIONS`.
- Tokens last about 60 days with no programmatic refresh; expiry is handled by telling the user to re-run `login`.
