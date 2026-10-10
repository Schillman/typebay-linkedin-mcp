"""Credential and token storage in the OS keychain. Env vars override client creds."""

import contextlib
import json
import os

import keyring
from keyring.errors import PasswordDeleteError

from .config import SERVICE


def _env_name(key: str) -> str:
    return f"LINKEDIN_{key.upper()}"


def get_client(key: str) -> str | None:
    """key is 'client_id' or 'client_secret'."""
    return os.environ.get(_env_name(key)) or keyring.get_password(SERVICE, key)


def set_client(key: str, value: str) -> None:
    keyring.set_password(SERVICE, key, value)


def load_token() -> dict | None:
    raw = keyring.get_password(SERVICE, "token")
    return json.loads(raw) if raw else None


def save_token(token: dict) -> None:
    keyring.set_password(SERVICE, "token", json.dumps(token))


def clear_all() -> None:
    for key in ("client_id", "client_secret", "token"):
        with contextlib.suppress(PasswordDeleteError):
            keyring.delete_password(SERVICE, key)
