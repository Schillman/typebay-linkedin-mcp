"""typebay-linkedin-mcp: publish LinkedIn posts from an MCP client."""

import argparse
import logging
import os
import sys
from importlib.metadata import version

from .config import DEFAULT_PORT


def main() -> None:
    p = argparse.ArgumentParser(prog="typebay-linkedin-mcp")
    p.add_argument("--version", action="version", version=version("typebay-linkedin-mcp"))
    p.add_argument(
        "command", nargs="?", default="serve", choices=["serve", "login", "logout", "status", "doctor"]
    )
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = p.parse_args()
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("LINKEDIN_MCP_LOG_LEVEL", "WARNING").upper())
    if args.command == "login":
        from .auth import login

        login(args.port)
    elif args.command == "logout":
        from . import store

        store.clear_all()
        print("Credentials and token removed from the keychain.", file=sys.stderr)
    elif args.command == "status":
        from .server import linkedin_status

        print(linkedin_status())
    elif args.command == "doctor":
        from . import doctor

        sys.exit(doctor.run(args.port))
    else:
        from .server import mcp

        mcp.run("stdio")
