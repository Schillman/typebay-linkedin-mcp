"""Local log of posts published by this server (LinkedIn offers no read back scope). Text and URNs only."""

import json
import os
from pathlib import Path


def path() -> Path:
    if d := os.environ.get("LINKEDIN_MCP_STATE_DIR"):
        return Path(d) / "posts.jsonl"
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "typebay-linkedin-mcp" / "posts.jsonl"


def record(entry: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _entries() -> list[dict]:
    try:
        lines = path().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # ponytail: skip a torn line rather than losing the whole log
    return out


def posts(limit: int = 20) -> list[dict]:
    entries = _entries()
    deleted = {e["deleted"] for e in entries if "deleted" in e}
    live = [e for e in entries if "urn" in e and e["urn"] not in deleted]
    return live[::-1][:limit]


def known(urn: str) -> bool:
    entries = _entries()
    deleted = {e["deleted"] for e in entries if "deleted" in e}
    return urn not in deleted and any(e.get("urn") == urn for e in entries)
