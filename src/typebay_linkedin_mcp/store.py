"""Credential and token storage in the OS keychain. Env vars override client creds."""

import contextlib
import json
import os

import keyring
from keyring.errors import KeyringError, PasswordDeleteError
from mcp.server.mcpserver.exceptions import ToolError

from .config import SERVICE


class StoreError(ToolError):
    """Keychain failure with a message that is safe to show the user."""


@contextlib.contextmanager
def _guard():
    try:
        yield
    except KeyringError as e:
        # class name only: backend text may leak details
        raise StoreError(
            f"Keychain unavailable ({type(e).__name__}). "
            "Fix: unlock or configure the OS keychain, then run: typebay-linkedin-mcp login"
        ) from None


def _env_name(key: str) -> str:
    return f"LINKEDIN_{key.upper()}"


def get_client(key: str) -> str | None:
    """key is 'client_id' or 'client_secret'."""
    if env := os.environ.get(_env_name(key)):
        return env
    with _guard():
        return keyring.get_password(SERVICE, key)


def set_client(key: str, value: str) -> None:
    with _guard():
        keyring.set_password(SERVICE, key, value)


def load_token() -> dict | None:
    with _guard():
        raw = keyring.get_password(SERVICE, "token")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        raise StoreError("Stored token is corrupt. Run: typebay-linkedin-mcp login") from None


def save_token(token: dict) -> None:
    with _guard():
        keyring.set_password(SERVICE, "token", json.dumps(token))


def clear_all() -> None:
    for key in ("client_id", "client_secret", "token"):
        with contextlib.suppress(PasswordDeleteError, KeyringError):
            keyring.delete_password(SERVICE, key)
