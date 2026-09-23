#!/usr/bin/env python
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from django.core.management import execute_from_command_line

    from config.telemetry import setup as setup_telemetry

    if len(sys.argv) > 1 and sys.argv[1] not in ("runserver", "test", "shell", "makemigrations"):
        setup_telemetry("job")

    execute_from_command_line(sys.argv)
