import keyring
import pytest
from keyring.errors import NoKeyringError
from mcp.server.mcpserver.exceptions import ToolError

from typebay_linkedin_mcp import server, store


def _boom(*args, **kwargs):
    raise NoKeyringError("backend text that must not leak")


@pytest.fixture
def broken_keyring(monkeypatch):
    monkeypatch.delenv("LINKEDIN_CLIENT_ID", raising=False)
    monkeypatch.delenv("LINKEDIN_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(keyring, "get_password", _boom)
    monkeypatch.setattr(keyring, "set_password", _boom)


def _check(e):
    msg = str(e.value)
    assert "NoKeyringError" in msg and "login" in msg
    assert "backend text" not in msg


@pytest.mark.parametrize(
    "call",
    [
        lambda: store.get_client("client_id"),
        lambda: store.set_client("client_id", "x"),
        store.load_token,
        lambda: store.save_token({"a": 1}),
    ],
)
def test_keyring_failure_becomes_store_error(broken_keyring, call):
    with pytest.raises(store.StoreError) as e:
        call()
    _check(e)


def test_corrupt_token(monkeypatch):
    monkeypatch.setattr(keyring, "get_password", lambda *a: "not json")
    with pytest.raises(store.StoreError, match="corrupt"):
        store.load_token()


def test_store_error_is_tool_error():
    assert issubclass(store.StoreError, ToolError)


def test_status_surfaces_store_error(broken_keyring):
    with pytest.raises(store.StoreError) as e:
        server.linkedin_status()
    _check(e)
