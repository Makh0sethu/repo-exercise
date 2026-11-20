"""Sentinel settings. Values for production come from environment variables."""
import importlib.util
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
    "django.contrib.staticfiles",
    "scanner",
]

_HAS_WHITENOISE = importlib.util.find_spec("whitenoise") is not None

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    *(["whitenoise.middleware.WhiteNoiseMiddleware"] if _HAS_WHITENOISE else []),
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "scanner.middleware.SecurityHeadersMiddleware",
    "scanner.middleware.RateLimitMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
}]

# Optional PostgreSQL database for caching public vulnerability/news data only.
if os.environ.get("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["POSTGRES_DB"],
            "USER": os.environ.get("POSTGRES_USER", "sentinel"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        }
    }
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": "sentinel_public_cache",
        }
    }
else:
    DATABASES = {}
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "sentinel-cache",
        }
    }

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
if _HAS_WHITENOISE and not DEBUG:
    STORAGES = {
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        }
    }

# Uploads: 1 MB per scan, kept in memory only and never written to disk.
SENTINEL_MAX_UPLOAD = 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = SENTINEL_MAX_UPLOAD + 64 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = SENTINEL_MAX_UPLOAD
FILE_UPLOAD_HANDLERS = ["django.core.files.uploadhandler.MemoryFileUploadHandler"]

# External API keys & rate limiting (public feeds only).
NVD_API_KEY = os.environ.get("NVD_API_KEY", "")
SENTINEL_RATE_LIMIT = int(os.environ.get("SENTINEL_RATE_LIMIT", "10"))
SENTINEL_PREVIEW_RATE_LIMIT = int(os.environ.get("SENTINEL_PREVIEW_RATE_LIMIT", "30"))
SENTINEL_TRUST_PROXY = os.environ.get("SENTINEL_TRUST_PROXY", "0") == "1"

# Security headers & HTTPS hardening
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CONTENT_TYPE_NOSNIFF = True
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = not DEBUG
if not DEBUG:
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SSL_REDIRECT", "1") == "1"

USE_TZ = True
TIME_ZONE = "UTC"
