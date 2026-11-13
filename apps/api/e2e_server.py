"""Run the Playwright API server with a larger listen backlog.

Django's `runserver` listens with a backlog of 10. A page of the SPA makes several requests at once and the suite runs
several workers, so on Windows the overflow showed up as ECONNREFUSED (a 500 from the Vite proxy) in a few runs out of
ten. This only changes the backlog (and turns development mode on); everything else is `runserver --noreload` on the given address.
"""

import os
import sys

from django.core.management import execute_from_command_line
from django.core.servers.basehttp import WSGIServer

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "softwarden.settings")
# Development mode, which defaults to off: the test servers run on the development secret key.
# playwright.config.ts sets it too, for the manage.py commands that run before this.
os.environ.setdefault("SOFTWARDEN_DEBUG", "1")
WSGIServer.request_queue_size = 256
address = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:8011"
execute_from_command_line(["manage.py", "runserver", address, "--noreload"])
