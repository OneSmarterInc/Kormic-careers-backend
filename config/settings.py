"""
Settings for the standalone careers dev project.

DEV SCAFFOLD — not what ships. The careers app drops into the real project,
which has its own settings module.

That said, the four settings the brief names as needing fixing before
careers.kormic.ai posts to a browser are done properly here rather than
repeated, so this file is a worked example of each:

  * SECRET_KEY is read from the environment. The fallback only applies with
    DEBUG on, and starting with DEBUG off and no key raises rather than
    silently running on a known-public value.
  * DEBUG defaults to False. Turning it on is an explicit act.
  * ALLOWED_HOSTS has no wildcard unless DEBUG is on.
  * CORS names origins. There is no CORS_ALLOW_ALL_ORIGINS anywhere in here,
    because an API that answers any origin with credentials attached is the
    same as having no origin policy at all.
"""
import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


DEBUG = _flag("DJANGO_DEBUG", False)

# A key that only exists when DEBUG is on. With DEBUG off and nothing in the
# environment this raises at import, which is the intended behaviour: a
# deployment running on a key that is committed to a repository is not secured
# by it.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise RuntimeError(
            "DJANGO_SECRET_KEY is not set. Set it, or run with DJANGO_DEBUG=1 "
            "for local development."
        )
    SECRET_KEY = "dev-only-insecure-key-do-not-deploy"

ALLOWED_HOSTS = _list("DJANGO_ALLOWED_HOSTS") or (["localhost", "127.0.0.1"] if DEBUG else [])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    # The app that ships.
    "careers",
    # Dev stubs. Both are replaced by the real thing on integration.
    "devauth",
    "django_api",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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

DATABASES = {
    "default": {
        "ENGINE": os.environ.get("DJANGO_DB_ENGINE", "django.db.backends.sqlite3"),
        "NAME": os.environ.get("DJANGO_DB_NAME", str(BASE_DIR / "dev.sqlite3")),
        "USER": os.environ.get("DJANGO_DB_USER", ""),
        "PASSWORD": os.environ.get("DJANGO_DB_PASSWORD", ""),
        "HOST": os.environ.get("DJANGO_DB_HOST", ""),
        "PORT": os.environ.get("DJANGO_DB_PORT", ""),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- API ------------------------------------------------------------------

REST_FRAMEWORK = {
    # JWT only. Session authentication is deliberately absent: with it, an
    # expired credential answers 403, and the client only refreshes on 401, so
    # a person would be signed out instead of renewed.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
}

SIMPLE_JWT = {
    # Short access, long refresh. The client renews in the background, so a
    # short window costs the person nothing and shrinks the value of a stolen
    # token — which matters because on web it is held in localStorage.
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=14),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# --- CORS -----------------------------------------------------------------

# Named origins, never a wildcard. Expo web serves on 8081 in development.
CORS_ALLOWED_ORIGINS = _list("DJANGO_CORS_ORIGINS") or (
    ["http://localhost:8081", "http://127.0.0.1:8081"] if DEBUG else []
)
CORS_ALLOW_CREDENTIALS = False

# --- Production hardening -------------------------------------------------

if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
