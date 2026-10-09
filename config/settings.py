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
import sys
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file() -> None:
    """
    Read `.env` beside manage.py, without overriding anything already exported.

    DEV SCAFFOLD, like the rest of this file. It exists because the alternative
    is exporting a handful of variables by hand in every new shell, and the
    syntax for that differs between PowerShell and Git Bash — which is a real
    source of "it works in one terminal and not the other".

    `setdefault`, never overwrite: a deployment that exports its own values
    keeps them, so this can never quietly beat a real environment.
    """
    path = BASE_DIR / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_env_file()


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

# --- Agents ---------------------------------------------------------------

# DEV ONLY. The helper bots live in the main project, and careers imports them
# lazily by name so a deployment without them still works. This puts that
# checkout on the path so they can be exercised here.
#
# In the real project careers sits alongside `agents` already and none of this
# is needed.
AGENTS_PATH = os.environ.get(
    "KORMIC_AGENTS_PATH", str(BASE_DIR.parent / "kormic-Django-Backend-Prajval-1")
)
# Appended, never prepended. That checkout also contains a `django_api` and an
# `accounts`, and putting it first shadows the stubs this project runs on —
# Django then tries to load the real models and their whole dependency tree.
# At the end of the path, only names this project does not already have
# resolve there, which is exactly `agents`.
if DEBUG and os.path.isdir(os.path.join(AGENTS_PATH, "agents")):
    sys.path.append(AGENTS_PATH)

# The shared agents package. Preferred over the in-repo adapter above, which
# wraps a resume parser written for graduate admissions — on a nurse's CV it
# goes looking for a GRE score.
#
# Appended for the same reason, and installed with `pip install -e` in a real
# deployment. This is the local-checkout convenience only.
KORMIC_AGENTS_PATH = os.environ.get(
    "KORMIC_AGENTS_PATH", str(BASE_DIR.parent / "kormic-agents")
)
if os.path.isdir(os.path.join(KORMIC_AGENTS_PATH, "kormic_agents")):
    sys.path.append(KORMIC_AGENTS_PATH)

# --- Logging --------------------------------------------------------------

# Django only configures its own `django` logger, so an app logger at INFO
# propagates to a root logger with no handler and is dropped. That silently
# swallowed the signup code, which `signup_start` logs in DEBUG precisely so
# the ladder can be walked locally without an email backend.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "{levelname} {name}: {message}", "style": "{"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "loggers": {
        "careers": {
            "handlers": ["console"],
            # INFO in development only. The code is not something to write into
            # a production log, and DEBUG being off is what stops it.
            "level": "INFO" if DEBUG else "WARNING",
            "propagate": False,
        },
    },
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


# Which model careers resolves a logged-in user to a person through. The dev
# project uses the devauth stub; the real project sets this to accounts.Account.
CAREERS_ACCOUNT_MODEL = "devauth.Account"


# --- OIG exclusion list (LEIE) ---------------------------------------------
# Where `manage.py fetch_leie` keeps the monthly download, and where the `oig`
# screening agent reads it. Not committed: it is 15MB of public data that is
# replaced every month.
LEIE_DIR = Path(os.environ.get("LEIE_DIR", BASE_DIR / "data" / "leie"))
LEIE_URL = os.environ.get(
    "LEIE_URL", "https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv"
)
