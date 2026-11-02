import pytest


@pytest.fixture(autouse=True)
def isolated_sessions(tmp_path, monkeypatch):
    """Point saved logins at a temporary folder: some code paths delete them."""
    folder = tmp_path / "sessions"
    folder.mkdir()
    monkeypatch.setattr("vccflow.portal.SESSIONS_DIR", folder)
    return folder
