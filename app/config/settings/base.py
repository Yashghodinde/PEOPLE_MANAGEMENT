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
DATABASES["default"]["CONN_MAX_AGE"] = 60
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
DATABASES["default"]["ATOMIC_REQUESTS"] = False

AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "/login"
OTS_HOME_URL = "/"
OTS_SELFIE_STAGE_ENABLED = False  # M06 enables the selfie step after the password
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
