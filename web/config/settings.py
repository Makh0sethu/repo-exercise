"""Sentinel settings. Values for production come from environment variables."""
import mimetypes
import os
from pathlib import Path

# Windows can map .js/.css to text/plain in the registry, and browsers then refuse
# the stylesheet and the Material Web module. Force the correct types.
mimetypes.add_type("text/css", ".css", True)
mimetypes.add_type("text/javascript", ".js", True)

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key" if DEBUG else "")
if not SECRET_KEY:
    raise RuntimeError("Set DJANGO_SECRET_KEY when DJANGO_DEBUG=0")
ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "audit",
    "pipeline",
    "scanner",
]

MIDDLEWARE = [
    "scanner.middleware.RequestTraceMiddleware",   # first: times and logs every request
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "accounts.middleware.GitHubAllowlistMiddleware",   # revokes access the moment someone is removed
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "scanner.context_processors.sentinel",
    ]},
}]

# SQLite on your machine; PostgreSQL on the server when SENTINEL_DB_NAME is set
# (also needs: pip install "psycopg[binary]").
if os.environ.get("SENTINEL_DB_NAME"):
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ["SENTINEL_DB_NAME"],
        "USER": os.environ.get("SENTINEL_DB_USER", ""),
        "PASSWORD": os.environ.get("SENTINEL_DB_PASSWORD", ""),
        "HOST": os.environ.get("SENTINEL_DB_HOST", "localhost"),
        "PORT": os.environ.get("SENTINEL_DB_PORT", "5432"),
    }}
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---- Usage tracing
# Admin lives at a path you choose, so scanners don't find it at /admin/.
SENTINEL_ADMIN_PATH = os.environ.get("SENTINEL_ADMIN_PATH", "admin/").strip("/") + "/"
# Only trust X-Forwarded-For when Sentinel sits behind your own proxy (nginx).
# Otherwise anyone can fake their IP with that header.
SENTINEL_TRUST_X_FORWARDED_FOR = os.environ.get("SENTINEL_TRUST_X_FORWARDED_FOR", "0") == "1"
# Secure cookies need HTTPS. On by default in production; set SENTINEL_HTTPS=0 to test
# a production-mode copy over plain http on your own machine (e.g. a pen test).
SENTINEL_HTTPS = os.environ.get("SENTINEL_HTTPS", "0" if DEBUG else "1") == "1"
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = SENTINEL_HTTPS
if SENTINEL_HTTPS:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if SENTINEL_TRUST_X_FORWARDED_FOR else None
SENTINEL_LOG_RETENTION_DAYS = int(os.environ.get("SENTINEL_LOG_RETENTION_DAYS", "30"))
# Only these git hosts may be cloned. Add your own GitHub Enterprise host if you use one.
PIPELINE_ALLOWED_GIT_HOSTS = [h for h in os.environ.get("SENTINEL_ALLOWED_GIT_HOSTS", "github.com").split(",") if h]
# Sign in with GitHub. Create an OAuth App (GitHub -> Settings -> Developer settings) whose
# callback URL is SENTINEL_BASE_URL + /auth/github/callback/
SENTINEL_BASE_URL = os.environ.get("SENTINEL_BASE_URL", "http://127.0.0.1:8000")
SENTINEL_GITHUB_CLIENT_ID = os.environ.get("SENTINEL_GITHUB_CLIENT_ID", "")
SENTINEL_GITHUB_CLIENT_SECRET = os.environ.get("SENTINEL_GITHUB_CLIENT_SECRET", "")
# Who may sign in, and as what: "alice:admin,bob:operator,carol:auditor". Empty = nobody.
SENTINEL_GITHUB_USERS = os.environ.get("SENTINEL_GITHUB_USERS", "")
# Where scanner binaries live, in addition to PATH (path-separator list).
PIPELINE_TOOL_DIRS = [d for d in os.environ.get("SENTINEL_TOOL_DIRS", "").split(os.pathsep) if d]
AUDIT_EVIDENCE_DIR = Path(os.environ.get("SENTINEL_EVIDENCE_DIR", BASE_DIR / "evidence"))
LOG_DIR = Path(os.environ.get("SENTINEL_LOG_DIR", BASE_DIR / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(message)s"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
        "access_file": {
            "class": "logging.handlers.RotatingFileHandler", "formatter": "plain",
            "filename": str(LOG_DIR / "access.jsonl"), "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5, "encoding": "utf-8",
        },
    },
    "loggers": {
        "sentinel.access": {"handlers": ["console", "access_file"], "level": "INFO", "propagate": False},
    },
}

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Uploads: 1 MB per scan, kept in memory only and never written to disk.
SENTINEL_MAX_UPLOAD = 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = SENTINEL_MAX_UPLOAD + 64 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = SENTINEL_MAX_UPLOAD
FILE_UPLOAD_HANDLERS = ["django.core.files.uploadhandler.MemoryFileUploadHandler"]

X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
USE_TZ = True
TIME_ZONE = "UTC"
