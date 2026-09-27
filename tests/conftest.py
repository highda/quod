import pytest


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Isolated harness root: state/, artifacts/, formal/ all resolve under tmp_path."""
    monkeypatch.setenv("QUOD_ROOT", str(tmp_path))
    (tmp_path / "formal").mkdir()
    return tmp_path
