"""Django settings for monkeurl.

Configuration is env-var first (12-factor); config.toml is a local-dev
fallback only and is NOT used in the container image.
"""

import os
import tomllib
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent

_config = {}
_toml = os.environ.get("MONKEURL_CONFIG", BASE_DIR / "config.toml")
try:
    with open(_toml, "rb") as f:
        _config = tomllib.load(f)
except (OSError, TypeError):
    pass


def env(name, default=None):
    return os.environ.get(name, default)


def env_int(name, default):
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def env_bool(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


_dj = _config.get("django", {})
_g = _config.get("google", {})

DEBUG = env_bool("DEBUG", _dj.get("DEBUG", False))
SECRET_KEY = env("SECRET_KEY", _dj.get(
    "SECRET_KEY", "django-insecure-dev-only-change-me"))
ALLOWED_HOSTS = env("ALLOWED_HOSTS", ",".join(_dj.get(
    "ALLOWED_HOSTS", ["localhost", "127.0.0.1"]))).split(",")

SITE_HOST = env("SITE_HOST", "localhost:8000")
CSRF_TRUSTED_ORIGINS = [o for o in env("CSRF_TRUSTED_ORIGINS", "").split(",")
                        if o]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") \
    if env_bool("SECURE_PROXY_SSL", False) else None

# turn these on in .env once a real TLS-terminating proxy is in front
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", False)
SESSION_COOKIE_SECURE = env_bool("SECURE_COOKIES", False)
CSRF_COOKIE_SECURE = env_bool("SECURE_COOKIES", False)
SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "rest_framework",
    "monkeurl",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]

ROOT_URLCONF = "monkeurl.urls"

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
            ],
        },
    },
]

WSGI_APPLICATION = "monkeurl.wsgi.application"


# --- Database: DATABASE_URL wins, sqlite for bare local dev ---------------

def _db_from_url(url: str) -> dict:
    p = urlparse(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": p.path.lstrip("/"),
        "USER": p.username or "",
        "PASSWORD": p.password or "",
        "HOST": p.hostname or "localhost",
        "PORT": str(p.port or 5432),
        "CONN_MAX_AGE": 60,
        "OPTIONS": {"connect_timeout": 10},
    }


DATABASE_URL = env("DATABASE_URL", "")
DATABASES = {
    "default": _db_from_url(DATABASE_URL)
    if DATABASE_URL else {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {"timeout": 20},
        # in-memory sqlite uses table locks that ignore busy_timeout;
        # a real file serializes concurrent writers correctly in tests
        "TEST": {"NAME": "/tmp/monkeurl_test_db.sqlite3"},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation."
             "UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation."
             "MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

MAILERS = {
    "default": {"BACKEND": "django.core.mail.backends.console.EmailBackend"},
}

# --- auth / allauth ---------------------------------------------------------

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
SITE_ID = 1
SOCIALACCOUNT_ONLY = env_bool("SOCIALACCOUNT_ONLY", False)
SOCIALACCOUNT_LOGIN_ON_GET = True   # no interstitial "continue?" page
ACCOUNT_LOGOUT_ON_GET = True        # one click and you're out
ACCOUNT_EMAIL_VERIFICATION = "none"
LOGIN_REDIRECT_URL = "/"

_google = {"APP": {
    "client_id": env("GOOGLE_CLIENT_ID", _g.get("CLIENT_ID", "")),
    "secret": env("GOOGLE_CLIENT_SECRET", _g.get("CLIENT_SECRET", "")),
    "key": "",
}}
SOCIALACCOUNT_PROVIDERS = {"google": _google} \
    if _google["APP"]["client_id"] else {}

# --- monkeurl ---------------------------------------------------------------

REDIS_URL = env("REDIS_URL", "redis://127.0.0.1:6379/0")

SHORTENER_CODE_LEN = env_int("SHORTENER_CODE_LEN", 7)
SHORTENER_RATE_LIMIT = env_int("SHORTENER_RATE_LIMIT", 5)
SHORTENER_RATE_WINDOW = env_int("SHORTENER_RATE_WINDOW", 60)
SHORTENER_CACHE_LRU_SIZE = env_int("SHORTENER_CACHE_LRU_SIZE", 1024)
SHORTENER_CACHE_LRU_TTL = env_int("SHORTENER_CACHE_LRU_TTL", 60)
SHORTENER_URL_CACHE_TTL = env_int("SHORTENER_URL_CACHE_TTL", 300)
SHORTENER_CLICK_FLUSH_THRESHOLD = env_int("SHORTENER_CLICK_FLUSH_THRESHOLD", 100)
SHORTENER_FLUSH_INTERVAL = env_int("SHORTENER_FLUSH_INTERVAL", 300)
SHORTENER_DEFAULT_TTL_SECONDS = env_int("SHORTENER_DEFAULT_TTL_SECONDS", 0)
SHORTENER_MAX_TTL_SECONDS = env_int("SHORTENER_MAX_TTL_SECONDS",
                                    10 * 365 * 86400)
SHORTENER_REDIRECT_STATUS = env_int("SHORTENER_REDIRECT_STATUS", 301)
SHORTENER_STRICT_DESTINATION = env_bool("SHORTENER_STRICT_DESTINATION", True)
SHORTENER_OWN_HOSTS = [h.strip().lower() for h in env(
    "SHORTENER_OWN_HOSTS", SITE_HOST.split(":")[0]).split(",") if h.strip()]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
