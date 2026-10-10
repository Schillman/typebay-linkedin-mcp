"""OAuth 3 legged authorization code flow with a localhost callback."""

import base64
import getpass
import html
import json
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from . import store
from .api import error_message
from .config import API_BASE, AUTH_URL, DEFAULT_PORT, INTROSPECT_URL, SCOPES, TOKEN_URL


def redirect_uri(port: int) -> str:
    return f"http://localhost:{port}/callback"


def build_auth_url(client_id: str, state: str, port: int) -> str:
    q = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri(port),
            "state": state,
            "scope": SCOPES,
        }
    )
    return f"{AUTH_URL}?{q}"


def _bind_callback_server(state: str, port: int) -> tuple[HTTPServer, dict[str, str]]:
    """Bind the callback port BEFORE the browser opens, so a clash fails fast and clearly."""
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path != "/callback":  # favicon etc. must not consume the one real callback
                self.send_error(404)
                return
            qs = urllib.parse.parse_qs(url.query)
            ok = qs.get("state", [""])[0] == state and "code" in qs
            if ok:
                result["code"] = qs["code"][0]
            else:
                result["error"] = qs.get("error_description", ["state mismatch or no code"])[0]
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab." if ok else b"Authorization failed.")

        def log_message(self, *_):  # keep the code out of any log
            pass

    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        raise RuntimeError(
            f"Port {port} is already in use ({e.strerror}). Pick a free port with --port N "
            "and register http://localhost:N/callback in your LinkedIn app."
        ) from e
    server.timeout = 1
    return server, result


def _wait_for_code(server: HTTPServer, result: dict[str, str], timeout: int = 180) -> str:
    deadline = time.time() + timeout
    while not result and time.time() < deadline:
        server.handle_request()
    server.server_close()
    if "code" not in result:
        err = html.unescape(result.get("error", "timed out"))
        hint = ""
        if "scope" in err.lower():
            hint = (
                " Enable the products 'Share on LinkedIn' and 'Sign In with LinkedIn using "
                "OpenID Connect' on your app's Products tab, then run login again."
            )
        raise RuntimeError(f"Authorization failed: {err}.{hint}")
    return result["code"]


def exchange_code(code: str, client_id: str, client_secret: str, port: int, http=httpx) -> dict:
    resp = http.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri(port),
        },
    )
    if resp.status_code != 200:
        try:  # LinkedIn's error fields never contain our secrets
            body = resp.json()
            why = f"{body.get('error', '')}: {body.get('error_description', '')}".strip(": ")
        except ValueError:
            why = error_message(resp)
        raise RuntimeError(
            f"Token exchange failed ({resp.status_code}): {why or 'no detail'}. "
            "401 invalid_client means the Client ID/Secret do not belong together; "
            "also check the redirect URL matches exactly."
        )
    data = resp.json()
    out = {"access_token": data["access_token"], "expires_at": int(time.time()) + int(data["expires_in"])}
    if data.get("id_token"):
        out["id_token"] = data["id_token"]
    return out


def introspect(access_token: str, client_id: str, client_secret: str, http=httpx) -> dict | None:
    """LinkedIn's view of the token (status, scope, expires_at), or None if it cannot be had."""
    try:
        resp = http.post(
            INTROSPECT_URL,
            data={"client_id": client_id, "client_secret": client_secret, "token": access_token},
            timeout=5,
        )
        return resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):  # never echo the exception, it may carry request details
        return None


def member_from_id_token(id_token: str, client_id: str) -> dict | None:
    """Read sub and name from the ID token returned by the token endpoint.

    The token came straight from LinkedIn over TLS in the code exchange, so per OIDC Core
    3.1.3.7 signature checking is optional; issuer, audience and expiry are still checked.
    """
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return None
    ok = (
        claims.get("iss") == "https://www.linkedin.com"
        and claims.get("aud") == client_id
        and claims.get("exp", 0) > time.time()
        and claims.get("sub")
    )
    return {"author_urn": f"urn:li:person:{claims['sub']}", "name": claims.get("name", "")} if ok else None


def fetch_member(access_token: str, id_token: str | None = None, client_id: str = "", http=httpx) -> dict:
    if id_token and (member := member_from_id_token(id_token, client_id)):
        return member
    resp = http.get(f"{API_BASE}/v2/userinfo", headers={"Authorization": f"Bearer {access_token}"})
    if resp.status_code != 200:
        msg = error_message(resp)
        detail = f"{resp.status_code}: {msg}" if msg else str(resp.status_code)
        raise RuntimeError(
            f"Could not read your profile ({detail}). "
            "Is 'Sign In with LinkedIn using OpenID Connect' enabled on the app?"
        )
    info = resp.json()
    return {"author_urn": f"urn:li:person:{info['sub']}", "name": info.get("name", "")}


def login(port: int = DEFAULT_PORT) -> None:
    """Interactive. Secrets are typed here, never passed through an MCP tool."""
    print(f"Add this redirect URL to your LinkedIn app (Auth tab): {redirect_uri(port)}", file=sys.stderr)
    client_id = (store.get_client("client_id") or input("Client ID: ")).strip()
    client_secret = (store.get_client("client_secret") or getpass.getpass("Client Secret (hidden): ")).strip()
    for key in ("client_id", "client_secret"):  # an env var silently overrides the prompt
        if os.environ.get(f"LINKEDIN_{key.upper()}"):
            print(f"Note: using LINKEDIN_{key.upper()} from the environment.", file=sys.stderr)
    print(f"Client ID is {len(client_id)} chars, secret is {len(client_secret)} chars.", file=sys.stderr)
    state = secrets.token_urlsafe(24)
    server, result = _bind_callback_server(state, port)
    webbrowser.open(build_auth_url(client_id, state, port))
    print("Waiting for authorization in your browser...", file=sys.stderr)
    code = _wait_for_code(server, result)
    token = exchange_code(code, client_id, client_secret, port)
    id_token = token.pop("id_token", None)  # not stored, only used to read the member id
    try:
        token.update(fetch_member(token["access_token"], id_token, client_id))
    except RuntimeError as e:
        info = introspect(token["access_token"], client_id, client_secret)
        scopes = "unknown"
        if info:
            scopes = f"{info.get('scope', 'none')} (status: {info.get('status', 'unknown')})"
        raise RuntimeError(
            f"{e}\nDiagnostics: id_token returned: {'yes' if id_token else 'no'}; "
            f"scopes on the issued token: {scopes}"
        ) from None
    store.set_client("client_id", client_id)
    store.set_client("client_secret", client_secret)
    store.save_token(token)
    print(f"Logged in as {token['name']}. Token stored in the OS keychain.", file=sys.stderr)
