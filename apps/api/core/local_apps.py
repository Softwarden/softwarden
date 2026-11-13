"""Softwarden's own Django apps, wherever they sit under the API directory (core, catalog, ... and the optional
`ee` package's, when installed), read from the app registry rather than a hand-kept list, so a new app is covered by the tenancy
meta-tests, the migration guard and the upgrade manifest the moment it is installed."""

from __future__ import annotations

from pathlib import Path

from django.apps import AppConfig, apps
from django.conf import settings
from django.db import models

# Folders under the API directory that hold other people's code (a local virtual environment, installed packages).
_FOREIGN = {".venv", "venv", "site-packages", "dist-packages", "node_modules"}


def is_local(config: AppConfig) -> bool:
    base = Path(settings.BASE_DIR).resolve()
    path = Path(config.path).resolve()
    if not path.is_relative_to(base):
        return False
    return not any(part in _FOREIGN or part.startswith(".") for part in path.relative_to(base).parts)


def local_app_configs() -> list[AppConfig]:
    """Every installed app whose package is under the API directory, at any depth (ee/saml as well as catalog)."""
    return sorted((c for c in apps.get_app_configs() if is_local(c)), key=lambda c: c.label)


def local_models(include_auto_created: bool = False) -> list[type[models.Model]]:
    """Every model of the local apps; with include_auto_created, the through tables Django makes for a plain
    ManyToManyField too."""
    return [m for config in local_app_configs() for m in config.get_models(include_auto_created=include_auto_created)]
