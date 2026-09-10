import pytest

from allies_runtime import files


@pytest.fixture
def root_owned_publication_spool(monkeypatch):
    if files.os.name != "nt":

        def state_root(root):
            path = root / files._PUBLICATION_STATE_DIRECTORY
            path.mkdir(parents=True, exist_ok=True)
            return path

        monkeypatch.setattr(files.os, "chown", lambda *_args: None)
        monkeypatch.setattr(files, "_publication_state_root", state_root)
