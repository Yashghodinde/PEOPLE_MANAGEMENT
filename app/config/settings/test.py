from .base import *  # noqa: F403

DEBUG = False
# Keep the contract hasher (OtsArgon2Hasher) in tests; M00.3 asserts its parameters.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
