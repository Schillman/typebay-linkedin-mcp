# typebay-linkedin-mcp: plan

Goal: a shareable MCP server that lets Claude publish LinkedIn posts (text, images, first comment) to the user's own profile through LinkedIn's official API. Each person supplies their own credentials. First real use: publish the agentic workflow post.

## Verified vs unverified

Verified against Microsoft Learn (2026-10-10). Sources: SOL = learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/share-on-linkedin, OIDC = .../self-serve/sign-in-with-linkedin-v2, UGC = learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/ugc-post-api (titled "Legacy"), INTRO = learn.microsoft.com/en-us/linkedin/shared/authentication/token-introspection, REFRESH = .../authentication/programmatic-refresh-tokens, ACF = .../authentication/authorization-code-flow.
- Two routes exist (SOL, OIDC). Self-serve: "Share on LinkedIn" (`w_member_social`) and "Sign In with LinkedIn using OpenID Connect" (`openid profile`), posting via `POST /v2/ugcPosts`, images via `POST /v2/assets?action=registerUpload`. Vetted: the Community Management API owns `/rest/posts`, needs a verified company page, and must be the only product on its app.
- Visibility `PUBLIC`, `CONNECTIONS`; 150 requests per member per day (SOL). Native Articles cannot be created through the API.
- Tokens last 60 days. No programmatic refresh for self-serve apps, it is for approved Marketing partners (ACF, REFRESH). Re-running login before expiry skips the consent screen.
- Text limit is 3000 characters (UGC).
- `DELETE /v2/ugcPosts/{urn}` is idempotent, a repeat returns 204 (UGC).
- Reshare: same body as a text post plus `responseContext.parent` (UGC). Not built yet.
- Link share: `shareMediaCategory:"ARTICLE"`, `media:[{status, originalUrl, title, description}]` (SOL, UGC). Not built yet.
- `/v2/ugcPosts` and `/v2/assets` need only `X-Restli-Protocol-Version: 2.0.0`, no `Linkedin-Version` (SOL). That header is for `/rest/*`.
- `GET /v2/userinfo` returns `sub`, `name`, `given_name`, `family_name`, `picture`, `locale`, plus `email` and `email_verified` only with the `email` scope (OIDC).
- `POST https://www.linkedin.com/oauth/v2/introspectToken`: form body `client_id`, `client_secret`, `token`; returns `active`, `status`, `scope`, `created_at`, `expires_at`, `authorized_at`, `auth_type` (INTRO).

Unverified, settled by the live test:
- Multiple images in one ugcPost. Docs describe MultiImage only on `/rest/posts`.
- Comment endpoint. Docs show `/rest/socialActions/{urn}/comments` with an `object` body field and scope `w_member_social_feed`. The `/v2/socialActions/{urn}/comments` path we use is undocumented. Add `object` if it returns 400.
- Hashtag rendering of plain `#tag` in ugcPosts (docs show it only for `/rest/posts`).
- Whether `w_member_social` alone allows delete.
- `SYNCHRONOUS_UPLOAD` on `/v2/assets` (docs show it on `/rest/assets`).
- Video. The UGC error table says video creation is restricted to allowlisted apps. Do not build before a test upload.

## Architecture

- Local stdio MCP server. Python 3.12+, `uv`, official `mcp` SDK (FastMCP), `httpx`, `keyring`.
- Distribution: `uvx --from git+<repo> typebay-linkedin-mcp`, no clone or global install needed.
- stdout is protocol only. All logs go to stderr, with tokens never logged.

## Credential model (shareable)

Bring your own LinkedIn app (v1). Each user creates a LinkedIn app, then runs:

    typebay-linkedin-mcp login

The CLI prompts (hidden input) for Client ID and Client Secret, stores them in the OS keychain, opens the browser for OAuth (authorization code flow, `state` checked, localhost callback), and stores the token in the keychain. Credentials are never tool arguments, so they never land in a chat transcript. Env vars `LINKEDIN_CLIENT_ID` / `LINKEDIN_CLIENT_SECRET` are accepted as an override for CI.

Why not one shared TypeBay app: the client secret would ship inside a package anyone can read. A hosted OAuth broker would fix that but is a service to run and secure. That is v2, only if sharing outside a small group matters.

Scopes: `openid profile w_member_social`. Nothing else.

## Tools (few, outcome oriented, annotated)

| Tool | Annotations | Purpose |
|---|---|---|
| `linkedin_status` | read only | auth state, member name, days until token expiry |
| `linkedin_preview_post` | read only | validates text (3000 chars) and images, returns exact preview plus a confirm token (hash of content) |
| `linkedin_publish_post` | open world, not idempotent | publishes only if the confirm token matches the preview; returns post URN and URL |
| `linkedin_add_comment` | open world | first comment (for a link, keeping it out of the post body) |
| `linkedin_delete_post` | destructive | deletes a post, needs confirm |

The preview, then confirm gate is the key safety feature: Claude cannot publish text the user has not seen.

## Safety and best practices

- Image inputs: local path allowlist, extension plus magic byte check, size cap, so the tool cannot be used to upload arbitrary files.
- `Linkedin-Version` is not needed on `/v2` endpoints. Pin it, configurable by env, only if the server ever moves to `/rest`.
- 401 gives "token expired, run login", 429 gives backoff and a clear message. Retries only on idempotent calls, never on publish.
- No credential in any file, log, error message or commit. `gitleaks` pre-commit hook.
- Least privilege: one scope set, personal profile only, no company page, no scraping, no inbox access.
- MIT license, README with threat model, `SECURITY.md`, semver.

## Phases

0. Prerequisites (needs the user, see below).
1. Scaffold: `uv init`, package layout, `pyproject.toml` entry point, ruff.
2. Auth: `login` CLI, keychain storage, `/v2/userinfo` for the member id (`urn:li:person:<sub>`), `linkedin_status`.
3. Text posts: `preview` and `publish` with the confirm gate.
4. Images: initializeUpload, upload, single and multi image posts.
5. Comment and delete.
6. Tests: `respx` mocked HTTP. The harness must fail on a known bad case (publish without confirm, wrong token, oversized image) before it is trusted.
7. Packaging: README, `.mcp.json` example, `claude mcp add` command with env references.
8. Live test: publish a test post with restricted visibility, verify it in the UI, delete it.
9. Real run: publish the agentic workflow post (text plus images), then the first comment.

## Needs from the user

1. LinkedIn app, Products tab: "Share on LinkedIn" and "Sign In with LinkedIn using OpenID Connect" both show as added.
2. LinkedIn app, Auth tab: redirect URL `http://localhost:8976/callback` registered.
3. Client ID and Client Secret: typed by the user into the `login` prompt once built (`! typebay-linkedin-mcp login`), never pasted into chat.
4. Decisions: article vs post, repo name and visibility, final post text and images.
