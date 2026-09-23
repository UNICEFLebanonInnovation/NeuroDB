"""NeuroDB v3 settings. One module, driven entirely by environment variables.

Every value with a security impact has no production default: the process refuses to
start without DJANGO_SECRET_KEY and DATABASE_URL. See .env.example for the contract.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent
env = environ.Env()
if (BASE_DIR / ".env").exists():
    env.read_env(str(BASE_DIR / ".env"))

ENV = env("DJANGO_ENV", default="production")  # local | test | staging | production
DEBUG = env.bool("DJANGO_DEBUG", default=False)
SECRET_KEY = env("DJANGO_SECRET_KEY")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[] if not DEBUG else ["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])
SITE_NAME = "NeuroDB"

# ---------------------------------------------------------------------------- apps
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "django.contrib.sites",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.microsoft",
    "rest_framework",
    "django_htmx",
    "neurodb.accounts",
    "neurodb.core",
    "neurodb.indicators",
    "neurodb.facts",
    "neurodb.geo",
    "neurodb.library",
    "neurodb.partnerships",
    "neurodb.integrations",
    "neurodb.reports",
    "neurodb.web",
]
SITE_ID = 1

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "neurodb.web.middleware.PublicPagesLoginRequiredMiddleware",  # login required except PUBLIC_PAGES
    "allauth.account.middleware.AccountMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "neurodb.web.middleware.ContentSecurityPolicyMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "neurodb" / "web" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "neurodb.web.context_processors.site",
                "neurodb.web.context_processors.navigation",
            ],
        },
    }
]

# ---------------------------------------------------------------------------- database
DATABASES = {
    "default": env.db("DATABASE_URL", default="postgres://neurodb@127.0.0.1:5433/neurodb_v3"),
}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# The v2 tables are owned by the database, not by Django, until the team decides to take
# ownership (docs/DATA_MIGRATION.md). Tests flip this so the test database can be created.
LEGACY_TABLES_MANAGED = env.bool("LEGACY_TABLES_MANAGED", default=False)
LEGACY_APPS = [
    "users",
    "pivoting",
    "etools",
    "locations",
]  # v2 labels; packages are accounts/indicators/facts/geo/library/partnerships
if ENV == "test":
    LEGACY_TABLES_MANAGED = True  # the legacy migrations read this flag, so tests create the v2 tables

# ---------------------------------------------------------------------------- auth
AUTH_USER_MODEL = (
    "users.User"  # the v2 app label, so existing content types, permissions and migration rows stay valid
)
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "reports:overview"
LOGOUT_REDIRECT_URL = "account_login"
ACCOUNT_LOGIN_METHODS = {"username", "email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "username*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_ADAPTER = "neurodb.accounts.adapters.AccountAdapter"  # closes self-registration
SOCIALACCOUNT_ADAPTER = "neurodb.accounts.adapters.SocialAccountAdapter"  # links pre-registered users only
SOCIALACCOUNT_AUTO_SIGNUP = False
SOCIALACCOUNT_LOGIN_ON_GET = True
ENTRA_TENANT_ID = env("ENTRA_TENANT_ID", default="")
SOCIALACCOUNT_PROVIDERS = {
    "microsoft": {
        "APPS": [
            {
                "client_id": env("ENTRA_CLIENT_ID", default=""),
                "secret": env("ENTRA_CLIENT_SECRET", default=""),
                "settings": {"tenant": ENTRA_TENANT_ID or "organizations"},
            }
        ]
        if env("ENTRA_CLIENT_ID", default="")
        else [],
    }
}
SSO_ENABLED = bool(env("ENTRA_CLIENT_ID", default=""))
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
SESSION_COOKIE_AGE = 12 * 60 * 60
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"

# ---------------------------------------------------------------------------- API
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"]
    + (["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.UserRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"user": "600/min"},
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 200,
}

# ---------------------------------------------------------------------------- i18n / time
LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English"), ("ar", "Arabic"), ("fr", "French")]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = env("TIME_ZONE", default="Asia/Beirut")
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------- static / media
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "neurodb" / "web" / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
STORAGES = {
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
}
if env("AZURE_STORAGE_ACCOUNT", default=""):
    STORAGES["default"] = {
        "BACKEND": "storages.backends.azure_storage.AzureStorage",
        "OPTIONS": {
            "account_name": env("AZURE_STORAGE_ACCOUNT"),
            "account_key": env("AZURE_STORAGE_KEY"),
            "azure_container": env("AZURE_CONTAINER_MEDIA", default="media"),
            "expiration_secs": 600,
        },
    }
if ENV == "test":  # tests run without collectstatic, so no manifest exists
    STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}
WHITENOISE_MANIFEST_STRICT = False
DATA_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024

# ---------------------------------------------------------------------------- security
if ENV in ("staging", "production"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
X_FRAME_OPTIONS = "DENY"
ADMIN_URL_PATH = env("ADMIN_URL_PATH", default="manage/")

# ---------------------------------------------------------------------------- public access
# The landing page (/ for anonymous visitors, /welcome/ for everyone) is always public. Other pages
# stay behind sign-in unless listed in PUBLIC_PAGES. Only pages without partner-level or
# unpublished data can be opened; dashboards, reports and the internal API are never public.
PUBLICABLE_PAGES = ("library", "library_download", "maps", "population")
PUBLIC_PAGES = [f"reports:{name}" for name in env.list("PUBLIC_PAGES", default=[])]
_unknown_public = {p.split(":", 1)[1] for p in PUBLIC_PAGES} - set(PUBLICABLE_PAGES)
if _unknown_public:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        f"PUBLIC_PAGES may only contain {PUBLICABLE_PAGES}; got {sorted(_unknown_public)}"
    )
if "reports:library" in PUBLIC_PAGES and "reports:library_download" not in PUBLIC_PAGES:
    PUBLIC_PAGES.append("reports:library_download")
PUBLIC_LANDING_STATS = env.bool("PUBLIC_LANDING_STATS", default=True)  # aggregate counts only
SUPPORT_EMAIL = env("SUPPORT_EMAIL", default="")
USER_GUIDE_URL = env("USER_GUIDE_URL", default="")

# ---------------------------------------------------------------------------- upstream systems
ACTIVITYINFO_BASE_URL = env("ACTIVITYINFO_BASE_URL", default="https://www.activityinfo.org")
ACTIVITYINFO_TOKEN = env("ACTIVITYINFO_TOKEN", default="")
ETOOLS_BASE_URL = env("ETOOLS_BASE_URL", default="https://etools.unicef.org")
ETOOLS_TOKEN = env("ETOOLS_TOKEN", default="")
INTEGRATION_TIMEOUT_SECONDS = (10, 120)
SYNC_STALENESS_HOURS = env.int("SYNC_STALENESS_HOURS", default=30)

# ---------------------------------------------------------------------------- logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {
        "django.request": {"level": "WARNING"},
        "neurodb": {"level": env("LOG_LEVEL", default="INFO")},
    },
}

if DEBUG and ENV == "local" and env.bool("DEBUG_TOOLBAR", default=True):
    try:
        import debug_toolbar  # noqa: F401

        INSTALLED_APPS.append("debug_toolbar")
        MIDDLEWARE.insert(0, "debug_toolbar.middleware.DebugToolbarMiddleware")
        INTERNAL_IPS = ["127.0.0.1"]
    except ImportError:
        pass
