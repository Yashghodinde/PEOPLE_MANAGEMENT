from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent
env = environ.Env()

SECRET_KEY = env("DJANGO_SECRET_KEY", default="insecure-dev-key-change-me")  # noqa: S105
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
ROOT_URLCONF = "config.urls"
ASGI_APPLICATION = "config.asgi.application"

INSTALLED_APPS = [
    "ots.core",
    "ots.accounts",
    "ots.audit",
    "ots.storage",
    "ots.tasks",
    "ots.notifications",
    "ots.attendance",
    "ots.live",
    "ots.files",
    "ots.backup",
    "ots.ui",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "ots.core.middleware.RequestIdMiddleware",
    "ots.core.middleware.ErrorMappingMiddleware",
    "ots.accounts.middleware.MustChangePasswordMiddleware",
    "ots.accounts.middleware.HrFenceMiddleware",
    "ots.core.idempotency.IdempotencyMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "ots.ui.context.ui",
            ]
        },
    }
]
DATABASES = {"default": env.db("DATABASE_URL", default="postgres://localhost/ots")}
# 0: under ASGI every request has its own thread, so a kept-open connection is never reused and just piles up
# (it filled PostgreSQL's connection limit in testing). The worker, which has no requests, keeps its own.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("OTS_DB_CONN_MAX_AGE", default=0)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
DATABASES["default"]["ATOMIC_REQUESTS"] = False

AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "/login"
OTS_HOME_URL = "/"
OTS_LATE_GRACE_MINUTES = env.int("OTS_LATE_GRACE_MINUTES", default=10)
OTS_SELFIE_STAGE_ENABLED = env.bool(
    "OTS_SELFIE_STAGE_ENABLED", default=True
)  # the selfie step after the password
SESSION_COOKIE_AGE = 12 * 3600  # absolute ceiling
SESSION_COOKIE_HTTPONLY = True
# "Secure" cookies need HTTPS. Production keeps the default (True); a developer looking at the app over plain
# http://localhost sets OTS_COOKIE_SECURE=false, because Safari will not keep a Secure cookie on plain http.
SESSION_COOKIE_SECURE = env.bool("OTS_COOKIE_SECURE", default=True)
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
# Number of reverse proxies in front of the app (Caddy = 1; plain dev server = 0). With 1 or more we trust
# Caddy's X-Forwarded-Proto (so HTTPS requests are seen as secure and CSRF origin checks pass) and take the
# client IP for the audit log from the entry Caddy added, never from a header the client could forge.
OTS_TRUSTED_PROXY_HOPS = env.int("OTS_TRUSTED_PROXY_HOPS", default=0)
if OTS_TRUSTED_PROXY_HOPS > 0:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
PASSWORD_HASHERS = ["ots.core.hashers.OtsArgon2Hasher"]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"ctx": {"()": "ots.core.logging.ContextFilter"}},
    "formatters": {"json": {"()": "ots.core.logging.OtsJsonFormatter"}},
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "json",
            "filters": ["ctx"],
        }
    },
    "root": {"handlers": ["stdout"], "level": "INFO"},
    # httpx logs every request URL at INFO. A Graph uploadUrl carries a temporary access token in the URL,
    # so these loggers stay at WARNING: secrets must never reach the logs.
    "loggers": {
        "httpx": {"level": "WARNING"},
        "httpcore": {"level": "WARNING"},
    },
}
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
WHITENOISE_USE_FINDERS = DEBUG
WHITENOISE_AUTOREFRESH = DEBUG
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
OTS_DATA_DIR = Path(env("OTS_DATA_DIR", default="/srv/data"))


# --- Storage (M05) -------------------------------------------------------------------------------
OTS_STORAGE_BACKEND = env("OTS_STORAGE_BACKEND", default="fake")  # "graph" in production
OTS_STORAGE_FAKE_DIR = Path(env("OTS_STORAGE_FAKE_DIR", default=str(OTS_DATA_DIR / "fake-remote")))
OTS_MANIFEST_DIR = Path(env("OTS_MANIFEST_DIR", default=str(OTS_DATA_DIR / "manifests")))  # monthly manifests
OTS_SELFIE_DIR = Path(env("OTS_SELFIE_DIR", default=str(OTS_DATA_DIR / "selfies")))
OTS_REFERENCE_PHOTO_DIR = Path(
    env("OTS_REFERENCE_PHOTO_DIR", default=str(OTS_DATA_DIR / "selfies" / "reference"))
)  # one small JPEG per person
# --- Files (M08) ---------------------------------------------------------------------------------
OTS_STAGING_DIR = Path(env("OTS_STAGING_DIR", default=str(OTS_DATA_DIR / "staging")))  # uploads in progress
OTS_BLOB_DIR = Path(env("OTS_BLOB_DIR", default=str(OTS_DATA_DIR / "blobs")))  # the cache, by SHA-256
OTS_UPLOAD_MAX_MB = env.int("OTS_UPLOAD_MAX_MB", default=500)
# File-name rule (IMP-01): words that do not describe a file; shortest run of letters that counts as a word.
OTS_NAME_PLACEHOLDERS = env.list(
    "OTS_NAME_PLACEHOLDERS",
    default=[
        "untitled", "document", "doc", "file", "newfile", "scan", "scanned", "image", "img", "photo",
        "screenshot", "download", "sheet1", "book1", "page", "temp", "test",
    ],
)  # fmt: skip
OTS_NAME_MIN_WORD = env.int("OTS_NAME_MIN_WORD", default=3)
OTS_VOICE_MAX_SECONDS = env.int("OTS_VOICE_MAX_SECONDS", default=300)  # a voice note in a task discussion
OTS_VOICE_MAX_MB = env.int("OTS_VOICE_MAX_MB", default=8)
OTS_NEAR_DEADLINE_HOURS = env.int("OTS_NEAR_DEADLINE_HOURS", default=24)  # amber accent on a task card
OTS_UPLOAD_PIECE_BYTES = 8 * 1024 * 1024
OTS_CACHE_CAP_GB = env.float("OTS_CACHE_CAP_GB", default=25.0)
OTS_DISK_FREE_MIN_PERCENT = env.float("OTS_DISK_FREE_MIN_PERCENT", default=10.0)  # F-FL-3
OTS_VERSION = env("OTS_VERSION", default="dev")
OTS_UPLINK_MBPS = env.int("OTS_UPLINK_MBPS", default=50)
OTS_FIELD_ENCRYPTION_KEY = env("OTS_FIELD_ENCRYPTION_KEY", default="")  # encrypts uploadUrl at rest
GRAPH_TENANT_ID = env("GRAPH_TENANT_ID", default="")
GRAPH_CLIENT_ID = env("GRAPH_CLIENT_ID", default="")
GRAPH_CERT_PATH = env("GRAPH_CERT_PATH", default="/etc/ots/graph.pem")
GRAPH_KEY_PATH = env("GRAPH_KEY_PATH", default="/etc/ots/graph.key")
GRAPH_SITE_ID = env("GRAPH_SITE_ID", default="")
GRAPH_DRIVE_BUSINESS = env("GRAPH_DRIVE_BUSINESS", default="")
GRAPH_DRIVE_DR = env("GRAPH_DRIVE_DR", default="")

# Delivery receipts (D-052): after the first time a change not yet received shows "Not delivered yet";
# an URGENT one still not received after the second time tells the sender once.
OTS_RECEIPT_LATE_SECONDS = env.int("OTS_RECEIPT_LATE_SECONDS", default=120)
OTS_RECEIPT_URGENT_SECONDS = env.int("OTS_RECEIPT_URGENT_SECONDS", default=900)

OTS_OFFICE_NAME = env("OTS_OFFICE_NAME", default="Trop Organic Office")  # shown in the TV board header

# Backup (M04): the WAL archive script writes here, the worker ships from the spool.
OTS_WAL_LOCAL_DIR = Path(env("OTS_WAL_LOCAL_DIR", default=str(OTS_DATA_DIR / "wal-local")))
OTS_WAL_SPOOL_DIR = Path(env("OTS_WAL_SPOOL_DIR", default=str(OTS_DATA_DIR / "wal-spool")))
OTS_WAL_SPOOL_WARN_BYTES = env.int("OTS_WAL_SPOOL_WARN_BYTES", default=6 * 1024**3)
OTS_WAL_SPOOL_CRITICAL_BYTES = env.int("OTS_WAL_SPOOL_CRITICAL_BYTES", default=int(7.5 * 1024**3))
OTS_BACKUP_LOCAL_DIR = Path(env("OTS_BACKUP_LOCAL_DIR", default=str(OTS_DATA_DIR / "backup-local")))
OTS_BACKUP_STAGING_DIR = Path(env("OTS_BACKUP_STAGING_DIR", default=str(OTS_DATA_DIR / "backup-staging")))
OTS_BACKUP_AGE_PUBLIC_KEY_PATH = Path(env("OTS_BACKUP_AGE_PUBLIC_KEY_PATH", default="/etc/ots/backup.pub"))
# pg_basebackup connects as the replication-only role `ots_backup` (created by the first core migration)
OTS_BACKUP_DB_HOST = env("OTS_BACKUP_DB_HOST", default="db")
OTS_BACKUP_DB_PORT = env.int("OTS_BACKUP_DB_PORT", default=5432)
OTS_BACKUP_DB_USER = env("OTS_BACKUP_DB_USER", default="ots_backup")
OTS_BACKUP_DB_PASSWORD = env("OTS_BACKUP_DB_PASSWORD", default="")
OTS_BACKUP_BASE_TIMEOUT_SECONDS = env.int("OTS_BACKUP_BASE_TIMEOUT_SECONDS", default=2 * 3600)
