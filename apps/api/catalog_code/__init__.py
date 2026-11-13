"""Catalog-as-code (docs/CATALOG_AS_CODE.md): the catalog as YAML files in git.

A plain package, not a Django app: it has no models (so no migrations for upgrade_check to guard). Its routes are
included from api/urls.py and its commands, catalog_export and catalog_apply, live in imports/management/commands."""
