import pytest

from allies_runtime import files


@pytest.fixture
def root_owned_publication_spool(monkeypatch):
    if files.os.name != "nt":
        monkeypatch.setattr(files.os, "chown", lambda *_args: None)
