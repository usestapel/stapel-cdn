"""Canonical-prefix URLconf for contract emission (contract-pipeline.md §2).

Unlike stapel-forms/-chat/-search (whose own ``urls.py`` bakes the full
``api/v1/`` segment in), stapel-cdn's ``urls.py`` contributes only the
mandatory ``v1/`` sub-prefix and documents that the HOST mounts it under its
own ``.../api/`` prefix — exactly what stapel-example-monolith and
darom-fleet already do: ``path("cdn/api/", include("stapel_cdn.urls"))``.
Mirrored here: ``cdn/api/`` + the module's own ``v1/`` gives the canonical
``/cdn/api/v1/…`` prefix.

Declared separately from the test urlconf so the contract-emission mount can
never silently drift from the module's documented public mount recipe.
"""
from django.urls import include, path

urlpatterns = [
    path("cdn/api/", include("stapel_cdn.urls")),
]
