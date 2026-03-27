DEBUG = True
SECRET_KEY = "z9RHXGyNSaOS995jjsCWIUe0xtedPl8Ei4XHTUngKm8fJ91RKx82wjnW6ixDtpL8"
ALLOWED_HOSTS = ["localhost", "0.0.0.0", "127.0.0.1","neurodb.azurewebsites.net", "neuro-db.org" ]
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "",
    }
}
EMAIL_HOST = "localhost"
EMAIL_PORT = 1025
INTERNAL_IPS = ["127.0.0.1", "10.0.2.2"]

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'neurodbv4',
        'HOST': 'localhost',
        'USER': 'postgres',
        'PASSWORD': 'LCgiTMLPY5jD3pdi' 
    }
}