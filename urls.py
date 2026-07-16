"""Root URLconf for stapel-cdn — v1 canon mount (api-versioning.md §2, §6).

Canon: ``/<mod>/api/v1/...`` — the version segment sits right after ``api/``.
Hosts keep mounting ``include('stapel_cdn.urls')`` under their ``.../api/``
prefix; this module contributes the mandatory ``v1/`` sub-prefix. The actual
URL set (paths inside unchanged) lives in ``urls_v1.py``.
"""
from django.urls import include, path

urlpatterns = [
    path('v1/', include('stapel_cdn.urls_v1')),
]
