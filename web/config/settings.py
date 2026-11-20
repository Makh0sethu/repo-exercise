"""Vulnwatch settings. Values for production come from environment variables."""
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

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
}]

# PostgreSQL is used later as a cache of public vulnerability data (spec step 4+).
DATABASES = {}

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Uploads: 1 MB per scan, kept in memory only and never written to disk.
VULNWATCH_MAX_UPLOAD = 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = VULNWATCH_MAX_UPLOAD + 64 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = VULNWATCH_MAX_UPLOAD
FILE_UPLOAD_HANDLERS = ["django.core.files.uploadhandler.MemoryFileUploadHandler"]

X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = not DEBUG
USE_TZ = True
TIME_ZONE = "UTC"
