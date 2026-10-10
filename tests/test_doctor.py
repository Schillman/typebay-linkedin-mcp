import socket
import time

import httpx
import pytest
import respx

from typebay_linkedin_mcp import doctor, store

SECRET = "FAKE-SECRET-VALUE"  # noqa: S105
TOKEN = "FAKE-ACCESS-TOKEN"  # noqa: S105
URL = "https://www.linkedin.com/oauth/v2/introspectToken"
OK_BODY = {"status": "active", "scope": "openid,profile,w_member_social"}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.delenv("LINKEDIN_CLIENT_ID", raising=False)
    monkeypatch.delenv("LINKEDIN_CLIENT_SECRET", raising=False)
    creds = {"client_id": "FAKE-ID", "client_secret": SECRET}
    monkeypatch.setattr(store, "get_client", lambda k: creds.get(k))
    monkeypatch.setattr(
        store, "load_token", lambda: {"access_token": TOKEN, "expires_at": int(time.time()) + 30 * 86400 + 60}
    )
    return creds, monkeypatch


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _failed(port) -> dict[str, str]:
    return {n: d for n, ok, d in doctor.checks(port) if not ok}


@respx.mock
def test_all_green(setup, capsys):
    respx.post(URL).respond(200, json=OK_BODY)
    assert doctor.run(_free_port()) == 0
    out = capsys.readouterr().out
    assert "[ok] Client credentials: present (keychain)" in out and "30 days left" in out
    assert "FAIL" not in out and SECRET not in out and TOKEN not in out


def test_missing_creds(setup, capsys):
    creds, _ = setup
    creds.clear()
    assert doctor.run(_free_port()) == 1
    out = capsys.readouterr().out
    assert "[FAIL] Client credentials: missing: run login" in out
    assert "Introspection" not in out


@respx.mock
def test_env_creds_reported(setup):
    _, mp = setup
    mp.setenv("LINKEDIN_CLIENT_ID", "x")
    mp.setenv("LINKEDIN_CLIENT_SECRET", "y")
    respx.post(URL).respond(200, json=OK_BODY)
    assert ("Client credentials", True, "present (env)") in doctor.checks(_free_port())


def test_expired_local_token(setup):
    _, mp = setup
    mp.setattr(
        store, "load_token", lambda: {"access_token": TOKEN, "expires_at": int(time.time()) - 86400 * 2}
    )
    assert "expired" in _failed(_free_port())["Token"]


@respx.mock
def test_revoked_status(setup, capsys):
    respx.post(URL).respond(200, json={**OK_BODY, "status": "revoked"})
    assert doctor.run(_free_port()) == 1
    assert "token is revoked" in capsys.readouterr().out


@respx.mock
def test_missing_scope(setup):
    respx.post(URL).respond(200, json={"status": "active", "scope": "w_member_social"})
    assert "openid" in _failed(_free_port())["Introspection"]


def test_port_busy(setup, capsys):
    with respx.mock:
        respx.post(URL).respond(200, json=OK_BODY)
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            assert doctor.run(port) == 1
    out = capsys.readouterr().out
    assert "--port N" in out and "http://localhost:N/callback" in out


@respx.mock
def test_network_failure(setup, capsys):
    respx.post(URL).mock(side_effect=httpx.ConnectError("x"))
    assert doctor.run(_free_port()) == 1
    out = capsys.readouterr().out
    assert "could not reach LinkedIn, check network" in out and SECRET not in out
