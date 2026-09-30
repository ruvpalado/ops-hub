"""Ops Hub settings. Every secret and every environment-specific value comes from
the environment. Nothing in this file is a hardcoded credential.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

BASE_DIR = Path(__file__).resolve().parent.parent


def load_env(path: Path = BASE_DIR / ".env") -> None:
    """Read KEY=VALUE pairs from .env into os.environ without overriding real env.

    Eight lines instead of a dependency. Later occurrences of a key win, and an
    empty value (a "name only" placeholder) does not shadow a real value further
    down the file — which is exactly the shape dev_db.py appends into.
    """
    if not path.exists():
        return
    pairs: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        pairs[key.strip()] = value.strip()
    for key, value in pairs.items():
        if value:
            os.environ.setdefault(key, value)


load_env()


def env_str(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw == "":
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be a boolean-ish value, got {raw!r}")


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is not set. Ops Hub refuses to start without it rather than "
            f"falling back to a default that would differ between environments."
        )
    return value


def _database_from_url(url: str) -> dict:
    """Minimal DATABASE_URL parser. Returns a unix-socket-capable config.

    A dependency-free parser is deliberate: this is 20 lines, it is the only URL
    format the project accepts, and dj-database-url would add a dependency to
    parse a string this project already controls.
    """
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    options = {}
    if "sslmode" in query:
        options["sslmode"] = query["sslmode"][0]
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parts.path.lstrip("/")),
        "USER": unquote(parts.username or ""),
        "PASSWORD": unquote(parts.password or ""),
        # A unix-socket DSN passes the socket directory in ?host=, which wins over
        # the URL authority. Both forms are supported on purpose: local dev uses a
        # socket, production uses TCP.
        "HOST": query.get("host", [parts.hostname or ""])[0],
        "PORT": str(parts.port or ""),
        "OPTIONS": options,
        "CONN_MAX_AGE": 60,
    }


OPSHUB_ENV = env_str("OPSHUB_ENV", "development")

SECRET_KEY = _require("OPSHUB_SECRET_KEY")
DEBUG = OPSHUB_ENV == "development"

ALLOWED_HOSTS = [h for h in (env_str("OPSHUB_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver") or "").split(",") if h]

INSTALLED_APPS = [
    "core",
    "accounts",
    "audit",
    "calendar_app",
    "django.contrib.sessions",
    "django.contrib.auth",
    "django.contrib.contenttypes",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]

ROOT_URLCONF = "opshub.urls"
WSGI_APPLICATION = "opshub.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": [
            "django.template.context_processors.request",
        ]},
    }
]

DATABASES = {"default": _database_from_url(_require("DATABASE_URL"))}

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# django.contrib.admin stays out by design: the spec's /admin/ carries a role
# check that admin's own decorator list would not give in one place, and the
# exact user model (app_user) does not match Django's skeleton auth tables.
# The permission choke point lands in the step that builds it.

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"

# Google OAuth. Empty means "sign-in is not configured"; the start view says so
# out loud rather than redirecting to a half-formed URL.
GOOGLE_CLIENT_ID = env_str("GOOGLE_CLIENT_ID", "") or ""
GOOGLE_CLIENT_SECRET = env_str("GOOGLE_CLIENT_SECRET", "") or ""
GOOGLE_REDIRECT_URI = env_str("GOOGLE_REDIRECT_URI", "") or ""
# Workspace domains allowed to claim an account unaided. Empty means nobody:
# a missing allowlist must not read as "open to the internet".
ALLOWED_EMAIL_DOMAINS = [d.strip() for d in env_str("ALLOWED_EMAIL_DOMAINS", "").split(",") if d.strip()]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "redaction": {"()": "core.redaction.RedactionFilter"},
    },
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "plain",
            "filters": ["redaction"],
        }
    },
    "root": {"handlers": ["console"], "level": env_str("OPSHUB_LOG_LEVEL", "INFO")},
}

if OPSHUB_ENV == "test":
    # Keep test output readable; assertions are the signal, not log noise.
    LOGGING["root"]["level"] = "CRITICAL"
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    DATABASES["default"]["CONN_MAX_AGE"] = 0

if DEBUG:  # pragma: no cover
    pass
