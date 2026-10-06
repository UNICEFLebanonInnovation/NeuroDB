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
    "neurodb.review",
    "neurodb.donors",
    "neurodb.youth",
    "neurodb.education",
    "neurodb.cpd",
    "neurodb.knowledge",
    "neurodb.wellbeing",
    "neurodb.graph",
    "neurodb.insights",
    "neurodb.watch",
    "neurodb.help",
    "neurodb.fmm",
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
    "neurodb.donors.middleware.DonorScopeMiddleware",  # a donor account sees its page and nothing else
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
# Without Blob storage (AZURE_STORAGE_ACCOUNT, below) uploads are kept on this disk: in a container they
# are lost when it is replaced, so production sets the storage account (MEDIA_ROOT can point to a
# mounted share instead).
MEDIA_ROOT = Path(env("MEDIA_ROOT", default=str(BASE_DIR / "media")))
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

# ------------------------------------------------------------------- knowledge hub, what's new
KNOWLEDGE_INDEX_ON_SAVE = env.bool("KNOWLEDGE_INDEX_ON_SAVE", default=True)  # library/CPD documents
KNOWLEDGE_HUB_ON_NEW_DATA = env.bool("KNOWLEDGE_HUB_ON_NEW_DATA", default=True)  # rebuild after each sync
KNOWLEDGE_HUB_SETTLE_SECONDS = env.int("KNOWLEDGE_HUB_SETTLE_SECONDS", default=60)  # a burst: one rebuild
SITE_URL = env("SITE_URL", default="").rstrip("/")  # e.g. https://neurodb.example.org, for email links
# Email (the daily what's new note): e.g. smtp+tls://user:password@smtp.office365.com:587; empty: no email
EMAIL_URL = env("EMAIL_URL", default="")
if EMAIL_URL:
    vars().update(env.email_url("EMAIL_URL"))
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="") or "NeuroDB <noreply@localhost>"
DIGEST_EMAIL_ENABLED = bool(EMAIL_URL)

# ---------------------------------------------------------------------------- upstream systems
ACTIVITYINFO_BASE_URL = env("ACTIVITYINFO_BASE_URL", default="https://www.activityinfo.org")
ACTIVITYINFO_TOKEN = env("ACTIVITYINFO_TOKEN", default="").strip()  # a stray newline breaks the header
# Or the ActivityInfo account's email and password (HTTP basic auth); used instead of the token when set
ACTIVITYINFO_USERNAME = env("ACTIVITYINFO_USERNAME", default="").strip()
ACTIVITYINFO_PASSWORD = env.ENVIRON.get("ACTIVITYINFO_PASSWORD", "").strip("\r\n")
if ACTIVITYINFO_USERNAME.startswith("@Microsoft.KeyVault(") or ACTIVITYINFO_PASSWORD.startswith(
    "@Microsoft.KeyVault("
):  # an unresolved Key Vault reference is not a credential
    ACTIVITYINFO_USERNAME = ACTIVITYINFO_PASSWORD = ""
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
# Compiler (the education and youth registration platform): the youth indicator figures, counts of
# young people only (GET /api/youth/indicator-figures/). The token is a Compiler service account's
# (group "NeuroDB API"), from the environment or Key Vault only (secret compiler-api-token).
COMPILER_API_URL = env("COMPILER_API_URL", default="").strip().rstrip("/")
COMPILER_API_TOKEN = env.ENVIRON.get("COMPILER_API_TOKEN", "").strip()
if COMPILER_API_TOKEN.startswith("@Microsoft.KeyVault("):
    COMPILER_API_TOKEN = ""
COMPILER_YOUTH_YEARS = env.int("COMPILER_YOUTH_YEARS", default=2)  # this year and the ones before
# Makani and Bridging counts (GET /api/figures/): the current year or round and the counted ones before it
COMPILER_EDUCATION_YEARS = env.int("COMPILER_EDUCATION_YEARS", default=3)
# NeuroDB asks BMA to calculate before reading (BMA keeps no schedule) and checks every minute whether
# it is done (BMA allows the figures API 120 calls an hour), giving up after two hours.
COMPILER_RUN_POLL_SECONDS = env.int("COMPILER_RUN_POLL_SECONDS", default=60)
COMPILER_RUN_TIMEOUT_MINUTES = env.int("COMPILER_RUN_TIMEOUT_MINUTES", default=120)
INTEGRATION_TIMEOUT_SECONDS = (10, 120)
SYNC_STALENESS_HOURS = env.int("SYNC_STALENESS_HOURS", default=30)
# The in-app scheduler (admin → Scheduled jobs), run by the web workers. Off: nothing runs on a schedule.
SCHEDULER_ENABLED = env.bool("SCHEDULER_ENABLED", default=True)

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


def _optional_float(name: str) -> float | None:
    """A number, or None when the variable is unset or empty."""
    value = env(name, default="").strip()
    return float(value) if value else None


# Every AI feature shares the one OpenAI key; the AI use ledger (admin → AI use) counts their tokens per
# day. The background features stop using AI first when all of them together pass 80% of this daily
# total, so Ask NeuroDB keeps working. Prices in USD per million tokens are optional: when set, the
# ledger shows the cost too (check the current OpenAI price list).
AI_DAILY_TOKEN_SOFT_CAP = env.int("AI_DAILY_TOKEN_SOFT_CAP", default=3_000_000)
AI_PRICE_INPUT_PER_MTOK = _optional_float("AI_PRICE_INPUT_PER_MTOK")
AI_PRICE_CACHED_PER_MTOK = _optional_float("AI_PRICE_CACHED_PER_MTOK")
AI_PRICE_OUTPUT_PER_MTOK = _optional_float("AI_PRICE_OUTPUT_PER_MTOK")

# ---------------------------------------------------------------------------- Help assistant
# The in-app help chat (Ctrl+Shift+H on every page, and /help/): answers how NeuroDB works from the help
# guide (neurodb/help/guide) and the live settings of Monitoring insights, never programme data. On by
# default when an OpenAI key is set (it also needs AI_ASSISTANT_ENABLED); the help pages work without it.
# Questions per person per day (a declined question does not count); the shared AI_DAILY_TOKEN_SOFT_CAP
# and the OpenAI credit pause apply.
HELP_ENABLED = env.bool("HELP_ENABLED", default=bool(OPENAI_API_KEY))
HELP_PER_USER_PER_DAY = env.int("HELP_PER_USER_PER_DAY", default=20)

# ---------------------------------------------------------------------------- NeuroDB Watch (For you)
# The background assistant behind each person's "For you" page: every morning (scheduled job "watch",
# 07:45) and shortly after new data arrives it checks what is due soon and what needs someone, then
# tells each person once. Rules find things; the AI only writes the morning note (one call per
# audience) and looks into a few critical items, within the caps below. Off: nothing runs.
WATCH_ENABLED = env.bool("WATCH_ENABLED", default=True)
WATCH_AI = env.bool("WATCH_AI", default=True)  # also needs AI_ASSISTANT_ENABLED; off: plain wording only
WATCH_MODEL = env("WATCH_MODEL", default="").strip() or AI_ASSISTANT_MODEL  # empty: the assistant's
WATCH_DAILY_TOKEN_CAP = env.int("WATCH_DAILY_TOKEN_CAP", default=300_000)  # input + output, notes + look-ups
# About 10 morning notes, plus 3 look-ups of up to 4 rounds each
WATCH_MAX_MODEL_CALLS_PER_DAY = env.int("WATCH_MAX_MODEL_CALLS_PER_DAY", default=24)
# The background look-up: on the open critical items (newly critical or worse first), read-only tools
WATCH_INVESTIGATE_ENABLED = env.bool("WATCH_INVESTIGATE_ENABLED", default=True)
WATCH_INVESTIGATE_PER_DAY = env.int("WATCH_INVESTIGATE_PER_DAY", default=3)
WATCH_SETTLE_SECONDS = env.int("WATCH_SETTLE_SECONDS", default=600)  # a burst of new data: one quick pass
WATCH_QUICK_PASSES_PER_DAY = env.int("WATCH_QUICK_PASSES_PER_DAY", default=6)  # later ones wait for 07:45
WATCH_TIME_LIMIT_SECONDS = env.int("WATCH_TIME_LIMIT_SECONDS", default=900)  # a run stops between steps
WATCH_NEEDS_YOU_PER_DAY = env.int("WATCH_NEEDS_YOU_PER_DAY", default=5)  # per person; the rest stays listed
WATCH_GOOD_TO_KNOW_PER_DAY = env.int("WATCH_GOOD_TO_KNOW_PER_DAY", default=10)
WATCH_GRANT_MIN_UNSPENT = env.int("WATCH_GRANT_MIN_UNSPENT", default=10_000)  # USD, for expiring grants
WATCH_EMAIL = env.bool("WATCH_EMAIL", default=True)  # one morning email, once EMAIL_URL is set

# ---------------------------------------------------------------------------- Monitoring insights (FMM)
# The eTools field monitoring visits, their links and their quality (/fmm/, built in steps). The refresh
# (`manage.py fmm_refresh`) builds the visits after every Datamart sync and each morning, and reads which
# keys the field monitoring records hold (admin: Monitoring insights > Fields found). Off: the refresh
# does nothing.
FMM_ENABLED = env.bool("FMM_ENABLED", default=True)
# At the end of every eTools Datamart sync: "inline" runs the refresh in the sync's process,
# "background" starts it as its own process, "off" leaves it to the morning run ("false" means "off")
FMM_REFRESH_AFTER_SYNC = env("FMM_REFRESH_AFTER_SYNC", default="inline").strip().lower()
if FMM_REFRESH_AFTER_SYNC == "false":
    FMM_REFRESH_AFTER_SYNC = "off"
if FMM_REFRESH_AFTER_SYNC not in ("inline", "background", "off"):
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        f"FMM_REFRESH_AFTER_SYNC must be inline, background or off; got {FMM_REFRESH_AFTER_SYNC!r}"
    )
# Passes one refresh may make to serve the rescores asked for while it runs
FMM_REFRESH_MAX_PASSES = env.int("FMM_REFRESH_MAX_PASSES", default=3)
# Share of a dataset's records a candidate key must fill to be chosen before the keys listed after it
FMM_KEY_MIN_COVERAGE = env.float("FMM_KEY_MIN_COVERAGE", default=0.5)
if not 0 < FMM_KEY_MIN_COVERAGE <= 1:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        f"FMM_KEY_MIN_COVERAGE must be above 0 and at most 1; got {FMM_KEY_MIN_COVERAGE}"
    )
# The address of an activity in eTools for the visit page's "Open in eTools", with {id} for the activity
# id (e.g. https://etools.unicef.org/fm/activities/{id}/details). Blank hides the link until it is verified.
FMM_ETOOLS_ACTIVITY_URL = env("FMM_ETOOLS_ACTIVITY_URL", default="")
# Map: the distance (km) below which a visit's point and a planned location's point count as the same
# place; only precise points (a site, or a cadaster's own point) are compared this way
FMM_MATCH_KM = env.float("FMM_MATCH_KM", default=2.0)
if not FMM_MATCH_KM > 0:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(f"FMM_MATCH_KM must be above 0; got {FMM_MATCH_KM}")
# Knowledge hub: the visits that ended within this many months are added to it; an off-track or constrained
# visit added to it is news in What's new only when it ended within FMM_NEWS_DAYS days
FMM_HUB_MONTHS = env.int("FMM_HUB_MONTHS", default=24)
FMM_NEWS_DAYS = env.int("FMM_NEWS_DAYS", default=30)
# The AI brief and the chat of Monitoring insights. Off at deploy: switched on at go-live (docs/OPERATIONS.md)
# once the keys are checked in Fields found and a Preview and a Test run look right. It also needs
# AI_ASSISTANT_ENABLED and a published prompt version. The prompts, the per-person quotas, "narr", "comp" and
# the model parameters are not settings: they live in the versioned prompt (admin: Prompt versions).
FMM_AI = env.bool("FMM_AI", default=False)
# The model of a prompt version that names none; empty: the assistant's
FMM_MODEL = env("FMM_MODEL", default="").strip() or AI_ASSISTANT_MODEL
# "auto": send temperature/top-p when a version sets them and the model has not refused them; "off": never
FMM_SAMPLING = env("FMM_SAMPLING", default="auto").strip().lower()
if FMM_SAMPLING not in ("auto", "off"):
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(f"FMM_SAMPLING must be auto or off; got {FMM_SAMPLING!r}")
FMM_SAMPLING_RECHECK_DAYS = env.int(
    "FMM_SAMPLING_RECHECK_DAYS", default=30
)  # a refused parameter is tried again
# Monitoring insights' own daily tokens and model calls for the whole office: about 30-40 chat answers and 10
# Regenerates a day plus the nightly briefs. Raise AI_DAILY_TOKEN_SOFT_CAP with it.
FMM_DAILY_TOKEN_CAP = env.int("FMM_DAILY_TOKEN_CAP", default=1_200_000)
FMM_MAX_CALLS_PER_DAY = env.int("FMM_MAX_CALLS_PER_DAY", default=400)
FMM_CHAT_MAX_RUNNING = env.int("FMM_CHAT_MAX_RUNNING", default=4)  # chat answers at once on the whole site
FMM_HISTORY_ANSWER_CHARS = env.int("FMM_HISTORY_ANSWER_CHARS", default=1500)  # per earlier answer re-sent
FMM_NIGHTLY_MAX_INSIGHTS = env.int("FMM_NIGHTLY_MAX_INSIGHTS", default=12)  # briefs written per night at most
FMM_NIGHTLY_MIN_VISITS = env.int("FMM_NIGHTLY_MIN_VISITS", default=3)  # a section's nightly brief needs these
FMM_MIN_VISITS_FOR_AI = env.int("FMM_MIN_VISITS_FOR_AI", default=3)  # no AI call for a filter with fewer
FMM_NARRATIVE_CHARS = env.int(
    "FMM_NARRATIVE_CHARS", default=600
)  # per text sent (narrative, answer, snippet)
FMM_INSIGHTS_TIMEOUT_SECONDS = env.int("FMM_INSIGHTS_TIMEOUT_SECONDS", default=90)  # per brief call
FMM_PAYLOAD_RETENTION_DAYS = env.int(
    "FMM_PAYLOAD_RETENTION_DAYS", default=30
)  # then a brief's payload is blanked
FMM_RETENTION_DAYS = env.int("FMM_RETENTION_DAYS", default=180)  # briefs and chat questions kept
# The AI checks of the narrative quality rules (fmm.ai.checks): their own daily token cap (they also stop
# at 80% of AI_DAILY_TOKEN_SOFT_CAP across every feature) and the time limit of one check
FMM_RULES_DAILY_TOKEN_CAP = env.int("FMM_RULES_DAILY_TOKEN_CAP", default=2_000_000)
FMM_RULES_TIMEOUT_SECONDS = env.int("FMM_RULES_TIMEOUT_SECONDS", default=60)
# The AI review of completed action points and the AI content summaries of the action points page
# (fmm.ai.ap_review, fmm.ai.ap_summary): their own daily token cap (the nightly review also stops at 80%
# of AI_DAILY_TOKEN_SOFT_CAP across every feature) and the time limit of one call
FMM_AP_REVIEW_DAILY_TOKEN_CAP = env.int("FMM_AP_REVIEW_DAILY_TOKEN_CAP", default=300_000)
FMM_AP_REVIEW_TIMEOUT_SECONDS = env.int("FMM_AP_REVIEW_TIMEOUT_SECONDS", default=60)
# The exports of Monitoring insights (Excel workbook, Power BI package and live feed): the country written
# in their country_name column (FMS §13.2), and the requests one Power BI key may make an hour
FMM_COUNTRY_NAME = env("FMM_COUNTRY_NAME", default="Lebanon").strip() or "Lebanon"
FMM_POWERBI_REQUESTS_PER_HOUR = env.int("FMM_POWERBI_REQUESTS_PER_HOUR", default=120)

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
