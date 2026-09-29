"""WSGI entry point for gunicorn. Django-admin keeps the path off the CLI
so the import is unambiguous in every runner."""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "opshub.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()