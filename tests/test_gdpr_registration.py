"""Re-populating the app registry registers the GDPR provider once."""
from unittest.mock import patch

import pytest

def test_populating_the_apps_twice_registers_the_gdpr_provider_once():
    """Re-populating INSTALLED_APPS runs ready() again; it must not raise."""
    from django.apps.registry import Apps
    from django.conf import settings
    from stapel_core.gdpr import gdpr_registry

    from stapel_cdn.gdpr import CDNGDPRProvider

    Apps(installed_apps=settings.INSTALLED_APPS)
    Apps(installed_apps=settings.INSTALLED_APPS)
    media = [p for p in gdpr_registry.providers if p.section == CDNGDPRProvider.section]
    assert len(media) == 1


def test_a_foreign_provider_on_the_section_still_conflicts():
    from types import SimpleNamespace

    from stapel_core.gdpr import GDPRRegistry

    from stapel_cdn import apps as cdn_apps
    from stapel_cdn.gdpr import CDNGDPRProvider

    registry = GDPRRegistry()
    registry._providers.append(SimpleNamespace(section=CDNGDPRProvider.section))
    with patch("stapel_core.gdpr.gdpr_registry", registry):
        with pytest.raises(ValueError):
            cdn_apps.register_gdpr_provider()
