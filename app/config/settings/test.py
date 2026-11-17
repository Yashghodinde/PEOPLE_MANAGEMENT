from .base import *  # noqa: F403

DEBUG = False
# Keep the contract hasher (OtsArgon2Hasher) in tests; M00.3 asserts its parameters.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Most tests log in with a password only. The attendance tests switch the selfie step on.
OTS_SELFIE_STAGE_ENABLED = False

# Many tests upload tiny files named a.bin or b.xlsx. The file-name rule (IMP-01) is relaxed here so they keep
# working; tests of the rule itself use the `real_name_rule` fixture to switch the production values back on.
OTS_NAME_PLACEHOLDERS = []
OTS_NAME_MIN_WORD = 1
