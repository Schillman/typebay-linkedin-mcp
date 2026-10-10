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

Local stdio MCP server (official `mcp` SDK, `MCPServer`) that posts to the user's own LinkedIn profile via the self-serve UGC Post API (`/v2/ugcPosts`, `/v2/assets`), not the vetted `/rest/posts` API. Bring-your-own LinkedIn app; nothing secret ships in the package. `PLAN.md` holds the design rationale and the verified behaviors (live test 2026-10-10) and what is still unverified (hashtag rendering, video).

Module flow, all under `src/typebay_linkedin_mcp/`:
- `__init__.py`: CLI entry (`main`). Imports `auth`/`server` lazily per subcommand. `serve` runs `mcp.run("stdio")`. Configures logging to stderr, level from `LINKEDIN_MCP_LOG_LEVEL`.
- `server.py`: six tools and three prompts (`announcement`, `lessons_learned`, `link_with_first_comment`; prompts stay registered in read-only mode). Tools are plain functions (tests call them directly) returning TypedDict results, which the SDK publishes as `outputSchema`. Shared `_validate` for preview and publish. `linkedin_status` calls `auth.introspect` when client creds are in the keychain, else falls back to the stored expiry (`source` field says which). Holds the in-memory `_previews` dict (confirm_token to issue time).
- `safety.py`: pure validation (text length, visibility, image allowlist/magic bytes/size, links via `Link` and `validate_link`) and `confirm_token`, plus `lint_text` (advisory preview warnings) and `strip_metadata` (JPEG/PNG EXIF, XMP, IPTC, text chunks).
- `postlog.py`: local JSONL post log (under `LINKEDIN_MCP_STATE_DIR`) written on publish and delete; backs `linkedin_list_my_posts` and the known URN check.
- `doctor.py`: the `doctor` subcommand, checks creds, token, scopes and callback port with fix hints.
- `api.py`: `LinkedInClient`, a thin httpx wrapper. `_explain` maps 401, 403, 422 duplicate and 429 (throttle vs daily cap) to user safe messages and never echoes the raw body (logged at debug only).
- `auth.py`: OAuth authorization code flow for the `login` CLI (localhost callback, `state` check, code exchange, member lookup).
- `store.py`: OS keychain via `keyring`. `LINKEDIN_CLIENT_ID`/`LINKEDIN_CLIENT_SECRET` env vars override stored client creds.
- `config.py`: constants and env overrides (`LINKEDIN_MCP_IMAGE_DIRS`, `LINKEDIN_MCP_MAX_IMAGE_BYTES`).

## Invariants to preserve

- **Preview then publish gate.** `linkedin_publish_post` recomputes the token from text, image content hashes, alt texts, link fields and visibility, and requires a matching preview under 15 minutes old. Each preview publishes once (popped only after a successful publish). The token proves content equality, not consent: human approval comes from the MCP client's permission prompt, so publish/comment/delete must not be auto-allowed.
- **stdout is protocol only.** Log to stderr. The `status` subcommand is the only thing that prints to stdout, and it is not run under `serve`.
- **No secrets anywhere.** Credentials are never tool arguments, never logged, never in error messages. `LinkedInError` and `ValidationError` messages must be safe to show the user. Both must subclass the SDK's `ToolError`: the SDK replaces any other exception's text with "Error executing tool X", losing the message (a test covers it).
- **Never retry a publish.** Retries are only acceptable for idempotent calls.
- **Image safety.** Paths are resolved (symlinks followed) before the allowlist check; type is decided by magic bytes, not extension. Images and a link are mutually exclusive.
- **Metadata is stripped before upload; the token binds the original file hash, not the stripped bytes.**
- **The post log holds text and URNs only, never tokens.** A logging failure must not fail a publish (`_log_record` swallows `OSError`).
- **Tool names and schemas are a contract.** Renames break clients' `mcp__linkedin__<name>` permission rules. Schema changes must regenerate the snapshot.
- `LINKEDIN_MCP_READ_ONLY=1` must keep write tools unregistered (see `_write_tool` in `server.py`). `linkedin_delete_post` refuses without `confirm=true`.
- Scopes stay `openid profile w_member_social`. Visibility is only `PUBLIC` or `CONNECTIONS`.
- Tokens last about 60 days with no programmatic refresh; expiry is handled by telling the user to re-run `login`.
