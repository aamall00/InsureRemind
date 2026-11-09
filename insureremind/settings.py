"""Django settings for InsureRemind. All configuration comes from environment / .env."""
import os
import sys
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

from .llmconfig import load_llm_config

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

TESTING = "test" in sys.argv


def env(name, default=""):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    return [x.strip() for x in env(name, default).split(",") if x.strip()]


DEBUG = env_bool("DEBUG", False)

SECRET_KEY = env("SECRET_KEY")
if not SECRET_KEY:
    if DEBUG or TESTING:
        SECRET_KEY = "insecure-dev-key-do-not-use-in-production"
    else:
        raise ImproperlyConfigured("SECRET_KEY environment variable is required when DEBUG is false.")

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1" if DEBUG else "")
if TESTING:
    ALLOWED_HOSTS.append("testserver")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "policies",
    "notifications",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # static files in production
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",  # every page requires login
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "insureremind.urls"

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

WSGI_APPLICATION = "insureremind.wsgi.application"

# Tests never touch the real database: they use SQLite unless TEST_DATABASE_URL points at a
# Postgres server (use a direct connection there; Supabase's pooler keeps idle sessions that
# block dropping the test database).
if TESTING:
    _db_url = env("TEST_DATABASE_URL") or f"sqlite:///{BASE_DIR / 'db.sqlite3'}"
else:
    _db_url = env("DATABASE_URL") or f"sqlite:///{BASE_DIR / 'db.sqlite3'}"
DATABASES = {
    "default": dj_database_url.parse(
        _db_url, conn_max_age=60,
        conn_health_checks=True,  # pooled Postgres (e.g. Supabase) can drop idle connections
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-in"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Media is NEVER served publicly: there is deliberately no MEDIA_URL route.
MEDIA_URL = "/protected-media-not-served/"
MEDIA_ROOT = BASE_DIR / "media"
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
MAX_UPLOAD_BYTES = 10 * 1024 * 1024



def build_storages(environ, testing=False):
    """Django STORAGES dict. `environ` is a mapping (os.environ) so this is testable without reloading settings."""
    backend = (environ.get("STORAGE_BACKEND") or "local").strip().lower()
    if backend == "local":
        default = {"BACKEND": "django.core.files.storage.FileSystemStorage"}
    elif backend == "azure":
        conn = environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
        account = environ.get("AZURE_STORAGE_ACCOUNT_NAME", "")
        if not account and not conn:
            raise ImproperlyConfigured(
                "STORAGE_BACKEND=azure needs AZURE_STORAGE_ACCOUNT_NAME (or AZURE_STORAGE_CONNECTION_STRING).")
        options = {
            "azure_container": environ.get("AZURE_STORAGE_CONTAINER") or "documents",
            "overwrite_files": False,
            "expiration_secs": None,  # never generate SAS URLs: files are streamed through protected views
        }
        if account:
            options["account_name"] = account
        if conn:
            options["connection_string"] = conn  # local testing only
        else:
            from azure.identity import DefaultAzureCredential  # lazy: only needed on Azure
            options["token_credential"] = DefaultAzureCredential()
        default = {"BACKEND": "storages.backends.azure_storage.AzureStorage", "OPTIONS": options}
    else:
        raise ImproperlyConfigured(f"Unknown STORAGE_BACKEND {backend!r} (use 'local' or 'azure').")
    static = ("django.contrib.staticfiles.storage.StaticFilesStorage" if testing
              else "whitenoise.storage.CompressedManifestStaticFilesStorage")
    return {"default": default, "staticfiles": {"BACKEND": static}}


STORAGE_BACKEND = env("STORAGE_BACKEND", "local")
STORAGES = build_storages(os.environ, TESTING)

# Token for POST /internal/cron/send-reminders/ (empty disables the endpoint)
CRON_TOKEN = env("CRON_TOKEN")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# --- Security (production) ---
if not DEBUG and not TESTING:
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # App Service's health probe may call the instance over plain HTTP; a 301 would count as unhealthy.
    SECURE_REDIRECT_EXEMPT = [r"^healthz$"]
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "SAMEORIGIN"  # PDFs are embedded in an iframe on our own pages

# --- Email ---
EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", "localhost")
EMAIL_PORT = int(env("EMAIL_PORT", "587") or 587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "reminders@example.com")

# --- AI extraction (OpenRouter). Secret key from env; everything else from config/llm.toml ---
OPENROUTER_API_KEY = env("OPENROUTER_API_KEY")
LLM = load_llm_config()

# --- Agency / reminders ---
AGENCY_NAME = env("AGENCY_NAME", "Your Insurance Agency")
REMINDER_DAYS = sorted(
    {int(x) for x in env_list("REMINDER_DAYS", "30,15,7,1") if x.isdigit()}, reverse=True
)
AGENT_NOTIFY_EMAIL = env("AGENT_NOTIFY_EMAIL")
AGENT_NOTIFY_PHONE = env("AGENT_NOTIFY_PHONE")
REMINDER_MAX_ATTEMPTS = 3

# --- WhatsApp ---
WHATSAPP_BACKEND = env("WHATSAPP_BACKEND", "console")
WHATSAPP_API_VERSION = env("WHATSAPP_API_VERSION", "v21.0")
WHATSAPP_PHONE_NUMBER_ID = env("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_ACCESS_TOKEN = env("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_TEMPLATE_NAME = env("WHATSAPP_TEMPLATE_NAME", "policy_expiry_reminder")
WHATSAPP_TEMPLATE_LANG = env("WHATSAPP_TEMPLATE_LANG", "en")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
