"""GET /api/v1/schema: the OpenAPI document; a process builds its first one alone.

drf-spectacular resolves each registered extension's `target_class` lazily, the first time a schema is built, by
replacing the class attribute's dotted path with the class (or None when it isn't installed) without a lock
(`plumbing.OpenApiGeneratorExtension._load_class`). Two requests building the first schema of a process at once raced
on it: one thread found the path already replaced and failed with "'NoneType' object has no attribute 'startswith'",
a 500. The first build of a process holds a lock until it has finished; after that, builds run side by side."""

from __future__ import annotations

import threading

from drf_spectacular.utils import extend_schema
from drf_spectacular.views import SCHEMA_KWARGS, SpectacularAPIView

_building = threading.Lock()
_built = threading.Event()  # set once this process has built a schema: the extensions are loaded, no race is left


class SchemaView(SpectacularAPIView):
    @extend_schema(**SCHEMA_KWARGS)  # as the parent's get: the schema route describes itself the same way
    def get(self, request, *args, **kwargs):
        if _built.is_set():  # warm: builds run side by side
            return super().get(request, *args, **kwargs)
        with _building:
            response = super().get(request, *args, **kwargs)
            _built.set()
            return response
