"""The WSGI application gunicorn serves.

Django is set up first, then the optional trace export (softwarden/otel.py: a no-op that imports nothing unless
OTEL_EXPORTER_OTLP_ENDPOINT is set), and only then is the handler built: building it loads the middleware chain, and
the Django instrumentation adds its middleware to settings.MIDDLEWARE, so it must run before that. This is what
django.core.wsgi.get_wsgi_application does, with the trace set-up in between. Each gunicorn worker imports this module
after the fork, so each sets up its own exporter."""

import os

import django
from django.core.handlers.wsgi import WSGIHandler

from softwarden import otel

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "softwarden.settings")
django.setup(set_prefix=False)
otel.configure("web")
application = WSGIHandler()
