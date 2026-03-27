from pathlib import Path
import sys
import environ

ROOT_DIR = Path(__file__).resolve(strict=True).parent.parent
env = environ.Env()

READ_DOT_ENV_FILE = env.bool("DJANGO_READ_DOT_ENV_FILE", default=True)
if READ_DOT_ENV_FILE:
    # OS environment variables take precedence over variables from .env
    env.read_env(str(ROOT_DIR / ".env"))
# DEBUG = env.bool("DJANGO_DEBUG", False)
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en-us"
SITE_ID = 1
USE_I18N = True
USE_TZ = False
LOCALE_PATHS = [str(ROOT_DIR / "locale")]
ALLOWED_HOSTS = ["*", "neurodb-v2.azurewebsites.net", "neuro-db.org", "uni-leb-neourodb-tst.azurewebsites.net", "uni-leb-neourodb.azurewebsites.net"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
ROOT_URLCONF = "azureproject.urls"
WSGI_APPLICATION = "azureproject.wsgi.application"
INSTALLED_APPS = [
    'jazzmin',
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.sites",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize", # Handy template tags
    "django.contrib.admin",
    "django.forms",
    'dal', 
    'dal_select2', 
    'gtm',
    "crispy_forms",
    "crispy_bootstrap5",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    'allauth.socialaccount.providers.oauth2',
    'mptt',
    'import_export',
    'rest_framework',
    'rest_framework_swagger',
    'rest_framework.authtoken',
    'django_json_widget',
    'djmoney',
    'taggit',
    'users.apps.MyAppConfig',
    'activityinfo.apps.MyAppConfig',
    'etools.apps.MyAppConfig',
    'locations.apps.MyAppConfig',
    'pivoting.apps.MyAppConfig',
]

# Provider-specific config
SOCIALACCOUNT_PROVIDERS = {
    'oauth2': {
        'APP': {
            'client_id': '<your-client-id>',
            'secret': '<your-client-secret>',
            'key': ''
        },
        'AUTH_PARAMS': {
            'response_type': 'code'
        },
        'SCOPE': ['openid', 'profile', 'email'],
        'OAUTH_PKCE_ENABLED': True,
    }
}

MIGRATION_MODULES = {"sites": "contrib.sites.migrations"}
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
AUTH_USER_MODEL = "users.User"
LOGIN_URL = "account_login"
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.common.BrokenLinkEmailsMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware"
]
STATIC_ROOT = str(ROOT_DIR.joinpath('staticfiles'))
# STATIC_ROOT = "/static/"
STATIC_URL = "/static/"
STATICFILES_DIRS = [str(ROOT_DIR.joinpath('static')),]
STATICFILES_FINDERS = [
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
]
MEDIA_ROOT = str(ROOT_DIR.joinpath('media')) 
MEDIA_URL = "/media/"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [str(ROOT_DIR.joinpath('templates')),],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.template.context_processors.i18n",
                "django.template.context_processors.media",
                "django.template.context_processors.static",
                "django.template.context_processors.tz",
                "django.contrib.messages.context_processors.messages",
                "users.context_processors.allauth_settings",
            ],
        },
    }
]

DATA_UPLOAD_MAX_NUMBER_FIELDS = 5000

FORM_RENDERER = "django.forms.renderers.TemplatesSetting"
CRISPY_TEMPLATE_PACK = "bootstrap5"
CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"
FIXTURE_DIRS = (str(ROOT_DIR.joinpath('fixtures')),)
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
CSRF_TRUSTED_ORIGINS = ["https://neurodb-v2.azurewebsites.net", "https://neuro-db.org", "https://www.neuro-db.org", "https://uni-leb-neourodb-tst.azurewebsites.net", "https://uni-leb-neourodb.azurewebsites.net"]

SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = "SAMEORIGIN"
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_TIMEOUT = 5
ADMIN_URL = "admin/"
ADMINS = [("""Zaher Sarieddine, In2uitions""", "zaher.sarieddine@in2uitions.com")]
MANAGERS = ADMINS
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "%(levelname)s %(asctime)s %(module)s "
                      "%(process)d %(thread)d %(message)s"
        }
    },
    "handlers": {
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "stream": sys.stderr,  # <---- this sends logs to stderr
            "formatter": "verbose",
        }
    },
    "root": {
        "level": "INFO",
        "handlers": ["console"]
    },
}

########## CELERY
# INSTALLED_APPS += ['leb_epics.taskapp.celery.CeleryConfig']
# CELERY_BROKER_URL = env('CELERY_BROKER_URL', default='redis://localhost:6379/0')
# CELERY_BROKER_URL = 'redis://:fN13Yc3BkPg+QtBwX8zMyB9CiddvSoKPI+t1YZwxPtk=@compiler.redis.cache.windows.net:6379/0'
# CELERY_RESULT_BACKEND = 'django-db'
########## END CELERY

# ------------------------------------------------------------------------------
# django-allauth
# ACCOUNT_ALLOW_REGISTRATION = False
ACCOUNT_AUTHENTICATION_METHOD = "username"
# ACCOUNT_EMAIL_REQUIRED = True
# ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_ADAPTER = "users.adapters.AccountAdapter"
ACCOUNT_FORMS = {"signup": "users.forms.UserSignupForm"}
SOCIALACCOUNT_ADAPTER = "users.adapters.SocialAccountAdapter"
SOCIALACCOUNT_FORMS = {"signup": "users.forms.UserSocialSignupForm"}

from .jazzmin_ui_tweaks import *
from .jazzmin_settings import *
import os

# Redirect after login/logout
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/'

ACCOUNT_EMAIL_REQUIRED = False
ACCOUNT_EMAIL_VERIFICATION = 'none'
ACCOUNT_ALLOW_REGISTRATION = env.bool('DJANGO_ACCOUNT_ALLOW_REGISTRATION', False)

DJANGO_MIGRATE = 'on'

if os.name == 'nt':
    from .local import *
    INSTALLED_APPS += ["django_extensions"]  # noqa F405
    DATABASES["default"]["CONN_MAX_AGE"] = 120
    DEBUG=True
else:
    from .production import *
    INSTALLED_APPS += ["anymail"]  # noqa F405
    DATABASES["default"]["CONN_MAX_AGE"] = 120
    # MIDDLEWARE.append("allauth.account.middleware.AccountMiddleware")
    DEBUG = env.bool("DJANGO_DEBUG", False)

