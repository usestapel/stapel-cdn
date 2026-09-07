"""Localized error catalogs (``translations/errors.<lang>.json``) — the parity gate.

This module owns twelve ``error.*`` keys and, since 0.19.1, ships their
``ru``/``es`` catalogs. A host frontend that compiles the fleet's error
catalogs reads ``docs/errors.json`` for the codes and pairs each with the
owner's catalog; a code the registry declares and no catalog carries renders
as an English fallback in an otherwise translated deployment. Until 0.19.1
that was every cdn code.

The gate is parity in both directions, per shipped language: every owned key
is translated, nothing but owned keys is, and every localized text keeps the
canon's ``{param}`` slots. ``generate_error_keys`` re-checks the first half at
emission time (``check_registry_catalog_pairing``), so ``make contract-check``
goes red on the same gap; this file is the part that runs without the
emission harness.
"""
import json
from pathlib import Path

import pytest
from stapel_core.i18n.domains import params_of

from stapel_cdn.errors import CDN_ERRORS

REPO = Path(__file__).resolve().parent.parent
TRANSLATIONS = REPO / "translations"
#: The languages this module ships error catalogs in; en is the registry
#: literal.
TARGET_LANGUAGES = ["ru", "es"]


def _catalog(lang: str) -> dict[str, str]:
    path = TRANSLATIONS / f"errors.{lang}.json"
    assert path.is_file(), f"{path.name} is missing"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("lang", TARGET_LANGUAGES)
def test_every_owned_key_is_translated(lang):
    catalog = _catalog(lang)
    missing = sorted(k for k in CDN_ERRORS if k not in catalog)
    assert not missing, f"{lang} catalog missing {len(missing)} key(s): {missing}"
    empty = sorted(k for k, v in catalog.items() if not (v or "").strip())
    assert not empty, f"{lang}: empty translations: {empty}"


@pytest.mark.parametrize("lang", TARGET_LANGUAGES)
def test_this_module_translates_only_its_own_keys(lang):
    """No fleet-wide copies: a reader resolves a foreign key from its owner."""
    stray = sorted(k for k in _catalog(lang) if k not in CDN_ERRORS)
    assert not stray, f"{lang}: not this module's keys: {stray}"


@pytest.mark.parametrize("lang", TARGET_LANGUAGES)
def test_translations_preserve_placeholders(lang):
    for key, text in _catalog(lang).items():
        assert set(params_of(text)) == set(params_of(CDN_ERRORS[key])), f"{lang}: {key}"


def test_every_shipped_catalog_is_a_target_language():
    """A catalog dropped in for a language this gate does not divide by is
    a catalog nothing keeps complete."""
    shipped = {p.name for p in TRANSLATIONS.glob("errors.*.json")}
    assert shipped == {f"errors.{lang}.json" for lang in TARGET_LANGUAGES}
