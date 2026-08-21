"""Single-module Django settings for stapel-cdn.

One ``settings.configure(...)`` block serves two callers, which is the
point: the test suite and the contract-emission harness cannot drift apart
if there is nothing to drift.

  - ``conftest.py`` — the bare test mount (``stapel_cdn.tests.urls``);
  - ``_codegen.py`` / ``make contract`` — the CANONICAL mount
    (``stapel_cdn.codegen_urls`` → ``cdn/``; the module's own ``urls.py``
    bakes the ``api/v1`` segment in, so the full public prefix is
    ``/cdn/api/v1``), plus drf-spectacular and the production
    ``REST_FRAMEWORK`` block so the emitted schema matches what a real
    deployment serves.

``SPECTACULAR_SETTINGS`` is deliberately not set: drf-spectacular builds its
settings singleton at import time, before a ``configure()``-based harness can
populate it, so the emitter runs on drf defaults — the state every other
pair-backend's harness emits under (stapel-forms/_codegen_settings.py is the
canon this mirrors). The one knob that must still be forced,
``SCHEMA_PATH_PREFIX``, is patched on the singleton directly by the harness.
"""
from __future__ import annotations


def settings_kwargs(
    *,
    root_urlconf: str = "stapel_cdn.tests.urls",
    contract: bool = False,
) -> dict:
    """The ``settings.configure(**kwargs)`` for a single-module cdn instance."""
    if contract:
        # Mirror stapel_core.django.settings.REST_FRAMEWORK exactly (the
        # config a real deployment emits under). Inlined, not imported, to
        # dodge the import-time settings read.
        rest_framework = {
            "DEFAULT_AUTHENTICATION_CLASSES": [
                "stapel_core.django.jwt.authentication.JWTCookieAuthentication",
            ],
            "DEFAULT_PERMISSION_CLASSES": [
                "stapel_core.django.api.permissions.IsServiceRequest",
                "stapel_core.django.api.permissions.IsSuperUser",
            ],
            "DEFAULT_RENDERER_CLASSES": [
                "rest_framework.renderers.JSONRenderer",
                "rest_framework.renderers.BrowsableAPIRenderer",
            ],
            "DEFAULT_SCHEMA_CLASS": "stapel_core.django.openapi.schemas.PermissionAwareAutoSchema",
            "EXCEPTION_HANDLER": "stapel_core.django.api.errors.stapel_exception_handler",
        }
    else:
        rest_framework = None

    kwargs = dict(
        SECRET_KEY="test-secret-key-not-for-production",
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "django.contrib.sessions",
            "django.contrib.messages",
            "django.contrib.admin",
            "stapel_core.django.apps.CommonDjangoConfig",
            "stapel_core.django.users",
            "rest_framework",
            "drf_spectacular",
            "stapel_cdn",
        ],
        AUTH_USER_MODEL="users.User",
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        USE_TZ=True,
        ROOT_URLCONF=root_urlconf,
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            }
        },
        STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
        MEDIA_ROOT="/tmp/stapel_cdn_codegen_media",
        MIGRATION_MODULES={
            "users": None,
            "cdn": None,
        },
    )
    if rest_framework is not None:
        kwargs["REST_FRAMEWORK"] = rest_framework
    return kwargs


# The multi-module common path prefix drf-spectacular auto-detects when
# every pair-backend's schema is emitted inside an all-modules aggregate.
# Forced on the singleton by the harness so a single-module instance derives
# the same operationIds.
CODEGEN_SCHEMA_PATH_PREFIX = "/"
