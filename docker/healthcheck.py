"""Docker HEALTHCHECK: the liveness endpoint on the port gunicorn listens on."""

import os
import sys
import urllib.request

try:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/healthz/live/", timeout=4
    ) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
