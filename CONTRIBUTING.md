# Contributing

    uv sync
    uv run pytest
    uv run ruff check src tests
    gitleaks detect

Rules that PRs must keep (details in `CLAUDE.md`): the preview/confirm gate, stdout reserved for the protocol, no secrets in code, logs or errors, no retries on publish, minimal scopes. A new tool needs a `title`, annotations, a test that fails when its guard is removed, and a read-only mode decision.

Tool names are API. Renaming a tool is a breaking change: clients store permission rules keyed by `mcp__linkedin__<tool_name>`, so a rename silently drops users' allow and deny rules. A schema change (parameters, defaults, annotations, title) must regenerate the snapshot and be reviewed in the diff:

    UPDATE_TOOLSNAPS=1 uv run pytest tests/test_tools_snapshot.py
