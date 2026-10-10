"""`doctor`: check the usual setup failures and print a fix hint for each."""

import os
import socket
import sys
import time

from . import store
from .auth import introspect, redirect_uri
from .config import SCOPES

Check = tuple[str, bool, str]


def _creds() -> tuple[Check, str | None, str | None]:
    try:
        cid, secret = store.get_client("client_id"), store.get_client("client_secret")
    except Exception:  # keyring backend errors can carry details, never echo them
        return ("Client credentials", False, "keychain not readable: check your OS keychain"), None, None
    if not (cid and secret):
        return ("Client credentials", False, "missing: run login"), None, None
    env = [k for k in ("CLIENT_ID", "CLIENT_SECRET") if os.environ.get(f"LINKEDIN_{k}")]
    src = "env" if len(env) == 2 else "keychain"  # mixed counts as keychain, login prints the detail
    return ("Client credentials", True, f"present ({src})"), cid, secret


def checks(port: int) -> list[Check]:
    out: list[Check] = []
    v = sys.version_info
    out.append(("Python", v >= (3, 12), f"{v.major}.{v.minor}" if v >= (3, 12) else "need 3.12 or newer"))

    cred_check, cid, secret = _creds()
    out.append(cred_check)

    try:
        token = store.load_token()
    except Exception:
        token = None
    if token:
        days = int((token.get("expires_at", 0) - time.time()) // 86400)
        out.append(("Token", days >= 0, f"{days} days left" if days >= 0 else "expired: run login"))
    else:
        out.append(("Token", False, "missing: run login"))

    if cid and secret and token:
        info = introspect(token["access_token"], cid, secret)
        if info is None:
            out.append(("Introspection", False, "could not reach LinkedIn, check network"))
        else:
            status = info.get("status", "unknown")
            have = {s.strip() for s in str(info.get("scope", "")).replace(" ", ",").split(",")}
            missing = [s for s in SCOPES.split() if s not in have]
            if status != "active":
                out.append(("Introspection", False, f"token is {status}: run login"))
            elif missing:
                out.append(
                    (
                        "Introspection",
                        False,
                        f"missing scopes {', '.join(missing)}: enable both products, then run login",
                    )
                )
            else:
                out.append(("Introspection", True, "active, all scopes granted"))

    try:
        with socket.socket() as s:  # no SO_REUSEADDR: match what HTTPServer would hit
            s.bind(("127.0.0.1", port))
        out.append(("Callback port", True, f"{port} is free"))
    except OSError:
        out.append(
            (
                "Callback port",
                False,
                f"{port} is in use: pick another with --port N and register http://localhost:N/callback",
            )
        )

    out.append(
        (
            "Reminder",
            True,
            f"app Auth tab needs redirect URL {redirect_uri(port)}; Products tab needs "
            "'Share on LinkedIn' and 'Sign In with LinkedIn using OpenID Connect'",
        )
    )
    return out


def run(port: int) -> int:
    results = checks(port)
    for name, ok, detail in results:
        print(f"[{'ok' if ok else 'FAIL'}] {name}: {detail}")
    return 0 if all(ok for _, ok, _ in results) else 1
