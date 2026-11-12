import pytest


# ---- storage fixtures shared by tests/storage and tests/failure ---------------------------------------------
@pytest.fixture
def fake(tmp_path):
    from ots.storage.fake import LocalDirProvider

    return LocalDirProvider(tmp_path / "remote")


@pytest.fixture(autouse=True)
def _field_key(settings):
    """uploadUrl is stored encrypted; give every test a valid key."""
    from cryptography.fernet import Fernet

    settings.OTS_FIELD_ENCRYPTION_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _photo_dir(settings, tmp_path):
    """Reference photos go to a temporary folder in tests, never to /srv/data."""
    settings.OTS_REFERENCE_PHOTO_DIR = tmp_path / "reference-photos"
