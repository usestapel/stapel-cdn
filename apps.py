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
