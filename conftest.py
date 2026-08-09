"""Test configuration for stapel-cdn.

Note the fixture encoder: images are generated with **Pillow**, which is no
longer a dependency of the library at all (0.10 moved the whole image path to
libvips — see ``stapel_cdn.decoders``). That is deliberate, not a leftover.
Fixtures produced by the same library under test would let "vips writes exactly
what vips reads" hide a real decode bug, so the suite keeps an independent
encoder and installs it via the ``test`` extra.

HEIC is the exception: there is no reliable HEIC *encoder* to generate one at
test time (libvips builds routinely ship the decoder without the encoder), so a
real 749-byte 16x16 HEIC is embedded below. It is the format at the centre of
the defect this module's decoder rework fixes — a valid HEIC refused as
"Invalid image file" because the validator asked Pillow, which had no HEIF
support, while the pipeline behind it read HEIC natively through libvips.
"""
import base64

import pytest

#: A real 16x16 HEIC (ISO-BMFF, `ftyp` brand `heic`). Embedded rather than
#: committed as a binary so the bytes are reviewable in the diff.
TINY_HEIC = base64.b64decode(
    "AAAAJGZ0eXBoZWljAAAAAG1pZjFNaVBybWlhZk1pSEJoZWljAAABw21ldGEAAAAAAAAAIWhkbHIA"
    "AAAAAAAAAHBpY3QAAAAAAAAAAAAAAAAAAAAAJGRpbmYAAAAcZHJlZgAAAAAAAAABAAAADHVybCAA"
    "AAABAAAADnBpdG0AAAAAAAEAAAA4aWluZgAAAAAAAgAAABVpbmZlAgAAAAABAABodmMxAAAAABVp"
    "bmZlAgAAAQACAABFeGlmAAAAABppcmVmAAAAAAAAAA5jZHNjAAIAAQABAAAA5mlwcnAAAADFaXBj"
    "bwAAABNjb2xybmNseAACAAIABoAAAAAMY2xsaQDLAEAAAAAUaXNwZQAAAAAAAAAQAAAAEAAAAAlp"
    "cm90AAAAABBwaXhpAAAAAAMICAgAAABxaHZjQwEDcAAAALAAAAAAAB7wAPz9+PgAAAsDoAABABdA"
    "AQwB//8DcAAAAwCwAAADAAADAB5wJKEAAQAjQgEBA3AAAAMAsAAAAwAAAwAeoBQgQcCTDOIe5FlU"
    "3AgIGAKiAAEACUQBwGFyyEBTJAAAABlpcG1hAAAAAAAAAAEAAQaBAgMFhoQAAAAsaWxvYwAAAABE"
    "AAACAAEAAAABAAACPwAAAK4AAgAAAAEAAAH3AAAASAAAAAFtZGF0AAAAAAAAAQYAAAAGRXhpZgAA"
    "TU0AKgAAAAgABAEGAAMAAAABAAIAAAESAAMAAAABAAEAAAFCAAQAAAABAAACAAFDAAQAAAABAAAC"
    "AAAAAAAAAACqKAGvoROQGmG1UxXrw1rVoP4nlBgCHfR7QtW0hF7QfMEsi2T0ZsZKEnYz0ppMMDUS"
    "viHaeXDNnDewMP4fevi2NOpF/9pzUe6n7z0xv/O1URvrV4mPOzPUP2N6ETHYAziHottTxVEPh0sk"
    "C15sD04NPvhdfb4yFVzcge/4yQt+SxO1Xt//efL//ljT/+l9vZ//2RT/+jL//NXX+icc32ldk9O5"
    "OjZ8O4LZPNg="
)


def heic_decodable() -> bool:
    """Whether THIS libvips build can read HEIC.

    libvips is modular: compiled without libheif it has no ``heifload``. Tests
    that need real HEIC pixels skip on such a build — an environment
    capability, not a gate. The *mechanism* (a configured-but-undecodable
    extension producing checks.E004 and a 503, never "your file is invalid") is
    asserted unconditionally in test_decoders.py with the capability stubbed,
    so nothing about this defect depends on how CI's libvips was compiled.
    """
    from stapel_cdn import decoders

    return ".heic" in decoders.loadable_extensions()


@pytest.fixture
def poisoned_pyvips():
    """Force `import pyvips` to fail — a deployment with no decoder at all.

    Shared because three modules need it now: the boot checks, the decoder
    seam, and the upload endpoints. `sys.modules[name] = None` is how Python
    itself marks a failed import, so this reproduces the real thing rather than
    a mock of it.
    """
    import sys

    saved = sys.modules.get("pyvips")
    sys.modules["pyvips"] = None
    yield
    sys.modules.pop("pyvips", None)
    if saved is not None:
        sys.modules["pyvips"] = saved


@pytest.fixture
def tiny_heic_bytes():
    """The raw bytes of a real HEIC, with no skip attached."""
    return TINY_HEIC


@pytest.fixture
def tiny_heic():
    """A real HEIC upload, or a skip on a libvips build without HEIF."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    if not heic_decodable():
        pytest.skip("this libvips build has no heifload (no HEIC decoder)")
    return SimpleUploadedFile("a.heic", TINY_HEIC, content_type="image/heic")


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
