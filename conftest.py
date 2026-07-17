def pytest_configure(config):
    from django.conf import settings
    if not settings.configured:
        settings.configure(
            SECRET_KEY="test-secret-key-not-for-production",
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "django.contrib.sessions",
                "django.contrib.messages",
                "django.contrib.admin",
                "stapel_core.django.users",
                "rest_framework",
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
            ROOT_URLCONF="stapel_cdn.tests.urls",
            CACHES={
                "default": {
                    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                }
            },
            # In-memory bus — no Kafka/Redis broker needed
            STAPEL_BUS_BACKEND="stapel_core.bus.backends.memory.MemoryBus",
            REST_FRAMEWORK={
                "DEFAULT_AUTHENTICATION_CLASSES": [
                    "rest_framework.authentication.BasicAuthentication",
                    "rest_framework.authentication.SessionAuthentication",
                ],
            },
            MEDIA_ROOT="/tmp/stapel_cdn_test_media",
            # Skip migrations — create tables directly from models
            MIGRATION_MODULES={
                "users": None,
                "cdn": None,
            },
            # The shipped default is ("avatar",) only (cdn-modularity.md
            # §2.1/§5 — zero-infrastructure, no marketplace-specific types
            # baked in). This test suite's fixtures predate that and use
            # "product" throughout as a second, generic image type — kept
            # here so the *test environment* still exercises a
            # multi-type deployment without rewriting every fixture.
            STAPEL_CDN={"ASSET_TYPES": ("avatar", "product")},
        )
        import django
        django.setup()
