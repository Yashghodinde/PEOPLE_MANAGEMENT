import pytest
from ots.storage import factory
from ots.storage.errors import ConfigurationError
from ots.storage.fake import LocalDirProvider


@pytest.fixture(autouse=True)
def _fresh():
    factory.reset_provider()
    yield
    factory.reset_provider()


def test_fake_backend_is_the_default_in_tests(settings, tmp_path):
    settings.OTS_STORAGE_BACKEND = "fake"
    settings.OTS_STORAGE_FAKE_DIR = tmp_path / "r"
    p = factory.get_provider()
    assert isinstance(p, LocalDirProvider) and factory.get_provider() is p  # created once, then reused


def test_unknown_backend_is_a_clear_error(settings):
    settings.OTS_STORAGE_BACKEND = "dropbox"
    with pytest.raises(ConfigurationError, match="graph.*fake"):
        factory.get_provider()
