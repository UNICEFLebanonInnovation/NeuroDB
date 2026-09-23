import os

from django.core.wsgi import get_wsgi_application

from config.telemetry import setup as setup_telemetry

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
setup_telemetry("web")
application = get_wsgi_application()
