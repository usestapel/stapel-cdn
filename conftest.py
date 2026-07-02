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
            CDN_ALLOWED_IMAGE_EXTENSIONS=[
                ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif",
            ],
            CDN_ALLOWED_VIDEO_EXTENSIONS=[
                ".mp4", ".webm", ".mov", ".avi", ".mkv",
            ],
            # Skip migrations — create tables directly from models
            MIGRATION_MODULES={
                "users": None,
                "cdn": None,
            },
        )
        import django
        django.setup()
