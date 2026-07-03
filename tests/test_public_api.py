"""
Tests for the package-level public API (PEP 562 lazy exports).
"""
import os
import subprocess
import sys

import stapel_cdn


class TestLazyExports:
    def test_all_declares_public_api(self):
        assert stapel_cdn.__all__ == [
            'cdn_settings',
            'media_exists',
            'refs_sync',
            'text_watermark',
            'validate_image_file',
        ]

    def test_cdn_settings_resolves(self):
        from stapel_cdn.conf import cdn_settings

        assert stapel_cdn.cdn_settings is cdn_settings

    def test_comm_functions_resolve(self):
        from stapel_cdn.functions import media_exists, refs_sync

        assert stapel_cdn.media_exists is media_exists
        assert stapel_cdn.refs_sync is refs_sync
        assert callable(stapel_cdn.media_exists)
        assert callable(stapel_cdn.refs_sync)

    def test_validator_resolves(self):
        from stapel_cdn.validators import validate_image_file

        assert stapel_cdn.validate_image_file is validate_image_file

    def test_dir_includes_exports(self):
        listing = dir(stapel_cdn)
        for name in stapel_cdn.__all__:
            assert name in listing

    def test_unknown_attribute_raises(self):
        try:
            stapel_cdn.nonexistent_export
        except AttributeError as exc:
            assert 'nonexistent_export' in str(exc)
        else:
            raise AssertionError('expected AttributeError')


class TestImportWithoutDjangoSettings:
    def test_package_import_is_django_free(self):
        """`import stapel_cdn` must not import Django nor require settings."""
        env = {k: v for k, v in os.environ.items() if k != 'DJANGO_SETTINGS_MODULE'}
        code = (
            'import sys\n'
            'import stapel_cdn\n'
            'polluted = [m for m in sys.modules if m == "django" or m.startswith("django.")]\n'
            'assert not polluted, f"django imported at package import time: {polluted}"\n'
        )
        result = subprocess.run(
            [sys.executable, '-c', code],
            capture_output=True,
            text=True,
            env=env,
            cwd=os.path.dirname(sys.executable),
        )
        assert result.returncode == 0, result.stderr
