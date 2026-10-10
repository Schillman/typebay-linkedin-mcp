import base64
import json
import socket
import threading
import urllib.request

import httpx
import pytest
import respx

from typebay_linkedin_mcp.auth import (
    _bind_callback_server,
    _wait_for_code,
    build_auth_url,
    exchange_code,
    fetch_member,
    introspect,
    member_from_id_token,
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_busy_port_fails_fast_with_clear_message():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        with pytest.raises(RuntimeError, match="already in use"):
            _bind_callback_server("s", busy.getsockname()[1])


def _hit(port: int, path: str) -> int:
    try:
        return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


def test_callback_ignores_favicon_and_rejects_wrong_state():
    port = _free_port()
    server, result = _bind_callback_server("good-state", port)
    errors = []

    def run():
        try:
            _wait_for_code(server, result, 10)
        except RuntimeError as e:
            errors.append(str(e))

    t = threading.Thread(target=run)
    t.start()
    assert _hit(port, "/favicon.ico") == 404 and not result
    assert _hit(port, "/callback?code=x&state=evil") == 400
    t.join()
    assert "code" not in result and "Authorization failed" in errors[0]


def test_callback_happy_path():
    port = _free_port()
    server, result = _bind_callback_server("good-state", port)
    out = {}
    t = threading.Thread(target=lambda: out.update(code=_wait_for_code(server, result, 10)))
    t.start()
    assert _hit(port, "/favicon.ico") == 404
    assert _hit(port, "/callback?code=abc&state=good-state") == 200
    t.join()
    assert out["code"] == "abc"


def test_auth_url_has_state_scope_and_redirect():
    u = build_auth_url("cid", "st", 8976)
    assert "state=st" in u and "w_member_social" in u and "localhost%3A8976%2Fcallback" in u


def test_scope_error_gives_actionable_hint():
    port = _free_port()
    server, result = _bind_callback_server("st", port)
    errors = []

    def run():
        try:
            _wait_for_code(server, result, 10)
        except RuntimeError as e:
            errors.append(str(e))

    t = threading.Thread(target=run)
    t.start()
    q = (
        "error=unauthorized_scope_error"
        "&error_description=Scope+%26quot%3Bopenid%26quot%3B+is+not+authorized&state=st"
    )
    _hit(port, f"/callback?{q}")
    t.join()
    assert "&quot;" not in errors[0] and "Products tab" in errors[0]


@respx.mock
def test_exchange_failure_surfaces_linkedin_reason_not_secret():
    respx.post("https://www.linkedin.com/oauth/v2/accessToken").respond(
        401, json={"error": "invalid_client", "error_description": "Client authentication failed"}
    )
    with pytest.raises(RuntimeError) as e:
        exchange_code("c", "cid", "TOPSECRET", 8976)
    msg = str(e.value)
    assert "invalid_client" in msg and "Client authentication failed" in msg
    assert "TOPSECRET" not in msg


@respx.mock
def test_exchange_success_returns_token_with_expiry():
    respx.post("https://www.linkedin.com/oauth/v2/accessToken").respond(
        200, json={"access_token": "AT", "expires_in": 5184000}
    )
    out = exchange_code("c", "cid", "sec", 8976)
    # S105: comparing against a fake test token fixture, not a hardcoded secret.
    assert out["access_token"] == "AT" and out["expires_at"] > 5184000  # noqa: S105


def _jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


GOOD = {"iss": "https://www.linkedin.com", "aud": "cid", "exp": 9999999999, "sub": "abc123", "name": "T"}


def test_member_from_id_token_ok_and_rejects_bad_claims():
    assert member_from_id_token(_jwt(GOOD), "cid") == {"author_urn": "urn:li:person:abc123", "name": "T"}
    assert member_from_id_token(_jwt({**GOOD, "aud": "other"}), "cid") is None
    assert member_from_id_token(_jwt({**GOOD, "iss": "https://evil.example"}), "cid") is None
    assert member_from_id_token(_jwt({**GOOD, "exp": 1}), "cid") is None
    assert member_from_id_token("not-a-jwt", "cid") is None


@respx.mock
def test_userinfo_fallback_and_error_shows_status():
    route = respx.get("https://api.linkedin.com/v2/userinfo").respond(200, json={"sub": "xyz", "name": "N"})
    assert fetch_member("AT", None, "cid")["author_urn"] == "urn:li:person:xyz"
    assert route.calls.last.request.headers["Authorization"] == "Bearer AT"
    respx.get("https://api.linkedin.com/v2/userinfo").respond(403, text="denied")
    with pytest.raises(RuntimeError, match="403"):
        fetch_member("AT", _jwt({**GOOD, "aud": "other"}), "cid")  # bad id token falls back


@respx.mock
def test_introspect_returns_dict_none_on_failure_and_never_leaks_secret():
    route = respx.post("https://www.linkedin.com/oauth/v2/introspectToken").respond(
        200, json={"active": True, "status": "active", "scope": "w_member_social"}
    )
    out = introspect("AT", "cid", "TOPSECRET")
    assert out["scope"] == "w_member_social" and out["status"] == "active"
    assert b"token=AT" in route.calls.last.request.read()
    respx.post("https://www.linkedin.com/oauth/v2/introspectToken").respond(401)
    assert introspect("AT", "cid", "x") is None
    respx.post("https://www.linkedin.com/oauth/v2/introspectToken").mock(side_effect=httpx.ConnectError("x"))
    assert introspect("AT", "cid", "x") is None


@respx.mock
def test_auth_fallbacks_do_not_echo_raw_body():
    respx.post("https://www.linkedin.com/oauth/v2/accessToken").respond(500, text="DO-NOT-SHOW")
    with pytest.raises(RuntimeError) as e:
        exchange_code("c", "id", "sec", 8976)
    assert "no detail" in str(e.value) and "DO-NOT-SHOW" not in str(e.value)
    body = {"message": "nope", "x": "DO-NOT-SHOW"}
    respx.get("https://api.linkedin.com/v2/userinfo").respond(403, json=body)
    with pytest.raises(RuntimeError) as e:
        fetch_member("AT", None, "cid")
    assert "403: nope" in str(e.value) and "DO-NOT-SHOW" not in str(e.value)
