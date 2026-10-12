"""Settings of the timing runs (``scripts/parity/timing.py``): the application's own settings, run with
``DJANGO_ENV=local`` and ``DEBUG`` off, so the caches are production's (the shared page figures in the
database table ``neurodb_cache``, never in memory as in the tests, and no plain-data walk of each stored
value). The one change: static files are served by name, because nothing was collected on this machine
(production's manifest storage would refuse every ``{% static %}``). Looking a name up costs the same.
"""

from config.settings import *  # noqa: F403
from config.settings import STORAGES

STORAGES = {**STORAGES, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
