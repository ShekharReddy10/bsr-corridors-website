"""
BSR Corridors admin — Django settings.

Everything secret or environment-specific comes from environment variables.
See .env.example for the full list.
"""

import os
import sys
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def env_bool(key: str, default: bool = False) -> bool:
    return env(key, str(default)).lower() in {"1", "true", "yes", "on"}


# Load a local .env file in development (simple KEY=VALUE lines; real env vars win).
_DB_GIVEN = "DATABASE_URL" in os.environ  # given on the command line (not from .env) — see the test guard below
_dotenv = BASE_DIR / ".env"
if _dotenv.exists():
    for line in _dotenv.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))

DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = env("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("Set DJANGO_SECRET_KEY.")
    SECRET_KEY = "dev-only-insecure-key"

ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
if render_host := env("RENDER_EXTERNAL_HOSTNAME"):
    ALLOWED_HOSTS.append(render_host)
CSRF_TRUSTED_ORIGINS = [f"https://{h}" for h in ALLOWED_HOSTS if h not in {"localhost", "127.0.0.1"}]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "core",
    "cluster",
    "pg",
]

# Each cluster item's tables go to its own schema; logins and cluster tables stay in the main database.
DATABASE_ROUTERS = ["cluster.router.ClusterRouter"]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.LoginRequiredMiddleware",
    "cluster.middleware.ClusterMiddleware",
    "core.middleware.CurrentUserMiddleware",
    "core.middleware.NoIndexMiddleware",
]

ROOT_URLCONF = "config.urls"

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
                "core.context_processors.app",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Database: Postgres via DATABASE_URL (Supabase in production). Falls back to SQLite for quick local runs.
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        conn_health_checks=True,
    )
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Database-backed cache: shared by all workers (used for login throttling).
CACHES = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "cache_table"}}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "cluster_home"
LOGOUT_REDIRECT_URL = "login"

# Sessions: expire after 8 hours of inactivity.
SESSION_COOKIE_AGE = 8 * 60 * 60
SESSION_SAVE_EVERY_REQUEST = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"

LANGUAGE_CODE = "en-in"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = False
USE_TZ = True
DATE_FORMAT = "d-m-Y"
SHORT_DATE_FORMAT = "d-m-Y"
DATETIME_FORMAT = "d-m-Y H:i"
DATE_INPUT_FORMATS = ["%Y-%m-%d", "%d-%m-%Y"]

STATIC_URL = "/static/"  # absolute: cluster item pages live under /p/<code>/
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        if DEBUG
        else "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

# ── App settings ──────────────────────────────────────────────
HOTEL_NAME = env("HOTEL_NAME", "BSR Corridors")
CLUSTER_NAME = env("CLUSTER_NAME", "BSR Group of Hotels")
DB_LIMIT_MB = int(env("DB_LIMIT_MB", "500"))  # database size limit (Supabase free plan: 500 MB)
# Payment links (UPI, no gateway): the UPI ID that receives the money and the name shown in the guest's UPI app.
UPI_ID = env("UPI_ID")
UPI_NAME = env("UPI_NAME", HOTEL_NAME)
# Short links sent to guests (/s/<code>): the address they start with, e.g. the website https://bsrcorridors.com
# (its Netlify rules forward /s/* here). Empty: this app's own address.
SHORT_LINK_BASE = env("SHORT_LINK_BASE").rstrip("/")

# Fernet key for encrypting ID numbers at rest. Generate with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Keep it safe: without it, encrypted ID numbers (and JSON backups) cannot be read.
FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY")

# Shared secret for the scheduled backup endpoint (GitHub Actions sends it in X-Backup-Token).
BACKUP_TOKEN = env("BACKUP_TOKEN")

# Google Drive (OAuth for the owner's Gmail account — see README "Google Drive setup").
GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = env("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = env("GOOGLE_REFRESH_TOKEN")
# Other cluster items connect their own Drive by signing in on the web: a "Web application" OAuth client in the same
# Google Cloud project, with redirect URI https://<admin app>/cluster/drive/callback/.
GOOGLE_WEB_CLIENT_ID = env("GOOGLE_WEB_CLIENT_ID")
GOOGLE_WEB_CLIENT_SECRET = env("GOOGLE_WEB_CLIENT_SECRET")
GDRIVE_ROOT_FOLDER = env("GDRIVE_ROOT_FOLDER", "BSR Corridors Admin")
if sys.argv[1:2] == ["test"]:  # tests never touch the real Drive, even with the keys in .env
    GOOGLE_CLIENT_ID = GOOGLE_CLIENT_SECRET = GOOGLE_REFRESH_TOKEN = GOOGLE_WEB_CLIENT_ID = GOOGLE_WEB_CLIENT_SECRET = ""
    # …nor the real database: .env's DATABASE_URL is the live one (or a tunnel to it). Tests need a test database
    # given on the command line, e.g.  DATABASE_URL=sqlite:////tmp/t.sqlite3 python manage.py test core
    if not _DB_GIVEN:
        sys.exit("Refusing to run tests with DATABASE_URL from .env (live database). "
                 "Give a test one: DATABASE_URL=sqlite:////tmp/t.sqlite3 python manage.py test core")
# The Gmail account whose Drive holds backups/exports (pre-selected when connecting).
GOOGLE_ACCOUNT_EMAIL = env("GOOGLE_ACCOUNT_EMAIL", "bsrcorridors@gmail.com")

LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
