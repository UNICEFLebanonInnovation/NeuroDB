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
# Azure injects the platform host name: Container Apps (name + environment DNS suffix) and App Service
# (WEBSITE_HOSTNAME). Trust it automatically so a new environment works before a custom domain exists.
_platform_hosts = [
    f"{env('CONTAINER_APP_NAME')}.{env('CONTAINER_APP_ENV_DNS_SUFFIX')}"
    if env("CONTAINER_APP_NAME", default="") and env("CONTAINER_APP_ENV_DNS_SUFFIX", default="")
    else "",
    env("WEBSITE_HOSTNAME", default=""),
]
for _host in filter(None, _platform_hosts):
    if _host not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_host)
    if f"https://{_host}" not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(f"https://{_host}")
APP_VERSION = env("APP_VERSION", default="dev")  # set by the Docker build (git SHA)
SITE_NAME = "NeuroDB"

# ---------------------------------------------------------------------------- apps
INSTALLED_APPS = [
    "unfold.apps.BasicAppConfig",  # admin theme (templates only; the site is NeuroDBAdminSite below)
    "neurodb.web.apps.NeuroDBAdminConfig",  # django.contrib.admin with the NeuroDB admin site
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
    "neurodb.datamart",
    "neurodb.integrations",
    "neurodb.reports",
    "neurodb.assistant",
    "neurodb.web",
]
SITE_ID = 1

MIDDLEWARE = [
    "neurodb.web.middleware.HealthCheckMiddleware",  # first: probes skip host checks and HTTPS redirects
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
if ENV in ("staging", "production"):
    # Azure Database for PostgreSQL requires TLS; a sslmode in DATABASE_URL still wins.
    DATABASES["default"].setdefault("OPTIONS", {}).setdefault("sslmode", env("DB_SSLMODE", default="require"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# The v2 tables are managed models: their initial migrations describe them as v2 left them and
# schema changes are normal migrations (docs/DATA_MIGRATION.md, "Django owns the schema").
LEGACY_APPS = [
    "users",
    "pivoting",
    "etools",
    "locations",
]  # v2 labels; packages are accounts/indicators/facts/geo/library/partnerships

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
    _blob = {
        "account_name": env("AZURE_STORAGE_ACCOUNT"),
        "azure_container": env("AZURE_CONTAINER_MEDIA", default="media"),
        "expiration_secs": 600,
    }
    if env("AZURE_STORAGE_KEY", default=""):
        _blob["account_key"] = env("AZURE_STORAGE_KEY")
    else:
        # Managed identity in Azure (AZURE_CLIENT_ID picks a user-assigned identity); az login locally.
        from azure.identity import DefaultAzureCredential

        _blob["token_credential"] = DefaultAzureCredential()
    STORAGES["default"] = {"BACKEND": "storages.backends.azure_storage.AzureStorage", "OPTIONS": _blob}
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

# ---------------------------------------------------------------------------- admin theme (django-unfold)
# Callables are resolved per request, so static() runs after the storage is configured.
UNFOLD = {
    "SITE_TITLE": "NeuroDB admin",
    "SITE_HEADER": "NeuroDB",
    "SITE_SUBHEADER": "Administration",
    "SITE_URL": "/",
    "SITE_ICON": lambda request: _static("img/logo-mark.png"),
    "SITE_FAVICONS": [
        {
            "rel": "icon",
            "type": "image/png",
            "sizes": "64x64",
            "href": lambda request: _static("img/favicon.png"),
        },
    ],
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": True,
    "ENVIRONMENT": "neurodb.web.admin_site.environment_badge",
    "DASHBOARD_CALLBACK": None,  # the dashboard is built in NeuroDBAdminSite.index
    "STYLES": [lambda request: _static("css/admin.css")],
    "BORDER_RADIUS": "8px",
    "COLORS": {
        # NeuroDB blue (#446ab3 at 600), matching the public site.
        "primary": {
            "50": "#f1f5fb",
            "100": "#e1e9f6",
            "200": "#c8d6ee",
            "300": "#a1b9e1",
            "400": "#7596d0",
            "500": "#5579c0",
            "600": "#446ab3",
            "700": "#385892",
            "800": "#2f4a7a",
            "900": "#283e64",
            "950": "#1a2842",
        },
    },
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": False,
        "navigation": "neurodb.web.admin_site.sidebar_navigation",
    },
}


def _static(path):
    from django.templatetags.static import static

    return static(path)


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
# A public list page makes its quick views (and the library's files) public too.
_FOLLOWERS = {
    "reports:library": ("library_download", "library_item", "library_file", "library_cover"),
    "reports:maps": ("map_item",),
}
for _page, _names in _FOLLOWERS.items():
    if _page in PUBLIC_PAGES:
        PUBLIC_PAGES += [f"reports:{n}" for n in _names if f"reports:{n}" not in PUBLIC_PAGES]
PUBLIC_LANDING_STATS = env.bool("PUBLIC_LANDING_STATS", default=True)  # aggregate counts only
SUPPORT_EMAIL = env("SUPPORT_EMAIL", default="")
USER_GUIDE_URL = env("USER_GUIDE_URL", default="")

# ---------------------------------------------------------------------------- upstream systems
ACTIVITYINFO_BASE_URL = env("ACTIVITYINFO_BASE_URL", default="https://www.activityinfo.org")
ACTIVITYINFO_TOKEN = env("ACTIVITYINFO_TOKEN", default="").strip()  # a stray newline breaks the header
ETOOLS_BASE_URL = env("ETOOLS_BASE_URL", default="https://etools.unicef.org")
ETOOLS_TOKEN = env("ETOOLS_TOKEN", default="").strip()
# eTools Datamart (datamart.unicef.io): the eTools data of every country office, read with HTTP basic
# authentication. The username and password come from the environment or Key Vault only (secrets
# etools-username / etools-password); never put them in a file that is committed. A Key Vault
# reference that did not resolve arrives as the literal "@Microsoft.KeyVault(...)" text: treat it as
# unset. Only line breaks are removed from the password (a secret saved from a file ends with one),
# and it is read as-is: django-environ would treat a value starting with "$" as another variable's name.
ETOOLS_DATAMART_URL = env("ETOOLS_DATAMART_URL", default="https://datamart.unicef.io")
ETOOLS_DATAMART_API_VERSION = env("ETOOLS_DATAMART_API_VERSION", default="latest")
ETOOLS_DATAMART_COUNTRY = env("ETOOLS_DATAMART_COUNTRY", default="Lebanon")  # the country_name filter
ETOOLS_DATAMART_PAGE_SIZE = env.int("ETOOLS_DATAMART_PAGE_SIZE", default=500)
ETOOLS_DATAMART_REPORTING_YEARS = env.int(
    "ETOOLS_DATAMART_REPORTING_YEARS", default=3
)  # partner reports kept
# Business area code for the PRP datasets (found from the Datamart workspaces when empty)
ETOOLS_DATAMART_BUSINESS_AREA = env("ETOOLS_DATAMART_BUSINESS_AREA", default="").strip()
ETOOLS_USERNAME = env("ETOOLS_USERNAME", default="").strip()
ETOOLS_PASSWORD = env.ENVIRON.get("ETOOLS_PASSWORD", "").strip("\r\n")
if ETOOLS_USERNAME.startswith("@Microsoft.KeyVault(") or ETOOLS_PASSWORD.startswith("@Microsoft.KeyVault("):
    ETOOLS_USERNAME = ETOOLS_PASSWORD = ""
INTEGRATION_TIMEOUT_SECONDS = (10, 120)
SYNC_STALENESS_HOURS = env.int("SYNC_STALENESS_HOURS", default=30)

# ---------------------------------------------------------------------------- AI assistant (OpenAI API)
# Natural-language questions answered by an OpenAI GPT model (ChatGPT) over NeuroDB's own data through
# read-only tools, using the OpenAI Responses API (platform.openai.com). The key comes from the
# environment or Key Vault only. Surrounding whitespace (a secret saved from a file with a trailing
# newline) is removed: the key would otherwise make every request fail. An App Service Key Vault
# reference that did not resolve arrives as the literal "@Microsoft.KeyVault(...)" text: treat that
# as unset.
OPENAI_API_KEY = env("OPENAI_API_KEY", default="").strip()
if OPENAI_API_KEY.startswith("@Microsoft.KeyVault("):
    OPENAI_API_KEY = ""
AI_ASSISTANT_ENABLED = env.bool("AI_ASSISTANT_ENABLED", default=True) and bool(OPENAI_API_KEY)
AI_ASSISTANT_MODEL = env("AI_ASSISTANT_MODEL", default="gpt-5.5")
# Reasoning effort; which values a model accepts depends on the model (gpt-5.5 takes none, low,
# medium, high and xhigh). The list is the openai SDK's ReasoningEffort values, written out so that
# settings do not import the SDK (a test checks the two agree); a wrong value stops the start-up.
AI_ASSISTANT_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
AI_ASSISTANT_EFFORT = env("AI_ASSISTANT_EFFORT", default="medium")
AI_ASSISTANT_HOURLY_LIMIT = env.int("AI_ASSISTANT_HOURLY_LIMIT", default=30)  # questions per user per hour
AI_ASSISTANT_MAX_TOOL_ROUNDS = env.int("AI_ASSISTANT_MAX_TOOL_ROUNDS", default=8)
AI_ASSISTANT_TIME_LIMIT_SECONDS = env.int(
    "AI_ASSISTANT_TIME_LIMIT_SECONDS", default=180
)  # Container Apps ingress cuts a request at 240 s, App Service at 230 s
if AI_ASSISTANT_EFFORT not in AI_ASSISTANT_EFFORTS:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        f"AI_ASSISTANT_EFFORT must be one of {AI_ASSISTANT_EFFORTS}; got {AI_ASSISTANT_EFFORT!r}"
    )

# ---------------------------------------------------------------------------- logging
LOG_FORMAT = env("LOG_FORMAT", default="plain")  # "json" in Azure so Log Analytics can parse fields
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
        "json": {"()": "config.logging.JsonFormatter"},
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": LOG_FORMAT}},
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
