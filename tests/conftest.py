import pytest


@pytest.fixture(autouse=True)
def _isolated_state_dir(monkeypatch, tmp_path):
    # Publish/delete append to the post log; never let tests touch the real one.
    monkeypatch.setenv("LINKEDIN_MCP_STATE_DIR", str(tmp_path))
