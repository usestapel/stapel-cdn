"""App configuration for CDN app."""
from django.apps import AppConfig


class CdnConfig(AppConfig):
    """CDN app configuration."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'stapel_cdn'
    label = 'cdn'
    verbose_name = 'CDN'

    def ready(self):
        """Wire up the app's registries and checks.

        No decoder registration happens here any more. It used to import
        ``pillow_heif`` to teach Pillow about HEIF and swallow the ImportError
        when the package was absent — which is precisely how a deployment came
        to advertise .heic in ALLOWED_IMAGE_EXTENSIONS and refuse it on upload.
        The image path decodes with libvips now, which needs no registration and
        whose absence is reported by checks.E001 instead of being passed over.
        """
        from stapel_core.gdpr import gdpr_registry
        from .gdpr import CDNGDPRProvider
        gdpr_registry.register(CDNGDPRProvider())

        # Action subscriptions (in-process in a monolith, bus consumer in
        # microservices — same code, transport chosen by STAPEL_COMM).
        from . import actions  # noqa: F401

        # comm Function providers (cdn.media_exists, cdn.describe,
        # cdn.describe_many, cdn.import_from_url, cdn.refs_sync).
        # Idempotent even if ready() runs more than once: the module import
        # is cached and re-registering the same handler object is a no-op.
        from . import functions  # noqa: F401

        # Submodule system checks (tag "stapel_cdn"): images/video/recordings
        # binary probes (cdn-modularity.md §2.2/§3) — catches "enabled but
        # missing binary" at manage.py check / boot-smoke time.
        from . import checks as _checks  # noqa: F401
