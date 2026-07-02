"""App configuration for CDN app."""
from django.apps import AppConfig


class CdnConfig(AppConfig):
    """CDN app configuration."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'stapel_cdn'
    label = 'cdn'
    verbose_name = 'CDN'

    def ready(self):
        """Register HEIF support when the app is ready."""
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except ImportError:
            pass
        from stapel_core.gdpr import gdpr_registry
        from .gdpr import CDNGDPRProvider
        gdpr_registry.register(CDNGDPRProvider())

        # Action subscriptions (in-process in a monolith, bus consumer in
        # microservices — same code, transport chosen by STAPEL_COMM).
        from . import actions  # noqa: F401

        # comm Function providers (cdn.media_exists, cdn.refs_sync).
        # Idempotent even if ready() runs more than once: the module import
        # is cached and re-registering the same handler object is a no-op.
        from . import functions  # noqa: F401
