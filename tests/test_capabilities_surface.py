"""Drift gate for the `surface` section of ``docs/capabilities.json``.

stapel-cdn's own upload code is the reason this section exists at all:
``ImageAdminForm.clean_original()`` hand-rolls a ``PIL.Image.open()`` check
instead of calling ``validate_image_file`` — the validator this module
already ships, missing both the decompression-bomb cap and the shared
extension allowlist as a result. Nothing in the module's contract document
could say "here is the validator to call" until this section existed:
``axes`` describes what you may switch on and ``extension_points`` what you
may replace, neither answers "is there already a mechanism for X".

``surface`` names the plain-Python helpers a product is meant to call
directly — the SSRF-hardened fetcher behind the outbound-HTTP path
(fetch.py), the image validator (validators.py) and the built-in watermark
engine (watermarks.py). The entry set is derived by AST from the roots in
``docs/capabilities.meta.json`` — a new public function in one of those three
files shows up here by itself and fails emission until somebody explains it.

Deliberately NOT in this section: the ``cdn.describe`` /
``cdn.media_exists`` / ``cdn.import_from_url`` / ``cdn.refs_sync`` comm
Functions in ``functions.py``. Those are bus-dispatched RPCs a caller reaches
through ``stapel_core.comm.call(...)``, not symbols a product imports and
calls directly — and ``cdn.describe`` is already named where a product
actually looks for it, stapel-core's ``STAPEL_MEDIA_BACKEND`` extension
point.
"""
import json
from pathlib import Path

import pytest

try:
    import stapel_tools  # noqa: F401  (probe: the emitter must be importable)
except ImportError as exc:  # pragma: no cover - environment failure, not a branch
    # NOT pytest.importorskip. A drift gate that skips when its emitter is
    # missing reports `1 skipped`, exits 0, and disappears among a hundred
    # green tests — exactly how a validator could go unadopted with nothing
    # red anywhere to say so. A gate that cannot run has FAILED; it has not
    # passed.
    raise RuntimeError(
        "capabilities surface drift gate cannot run: stapel-tools is not "
        "importable, and it carries the capabilities emitter this gate "
        "measures drift against. Install it (workspace venv, or `pip install "
        "stapel-tools`) and re-run. This is a hard failure on purpose — a "
        "skipped drift gate is silently no gate."
    ) from exc

from stapel_tools.surface import _stable_json, load_meta, patch_capabilities  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
COMMITTED = REPO / "docs" / "capabilities.json"

SECURITY_GATES = {
    "fetch_image_bytes",
    "detect_image_extension",
    "validate_image_file",
}


def _emitted() -> dict:
    try:
        return patch_capabilities(REPO, load_meta(REPO))
    except SystemExit as exc:  # the LOUD rule — report it, don't bury it
        pytest.fail(f"capabilities emission refused: {exc}", pytrace=False)


def test_no_drift():
    assert COMMITTED.read_text() == _stable_json(_emitted()), (
        "docs/capabilities.json is stale — run `make contract` and commit it"
    )


def test_version_tracks_pyproject():
    """The document carries the module version. cdn's hand-written copy had
    already rotted once (0.8.1 shipped claiming 0.8.0) before --patch existed
    to derive it."""
    import tomllib

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert json.loads(COMMITTED.read_text())["version"] == (
        pyproject["project"]["version"]
    )


def test_every_security_gate_is_named_and_explained():
    surface = json.loads(COMMITTED.read_text())["surface"]
    by_name = {e["name"]: e for e in surface}
    assert SECURITY_GATES <= set(by_name)
    for name in SECURITY_GATES:
        entry = by_name[name]
        assert entry["kind"] == "gate_function", entry
        assert entry["intent"].strip(), entry


def test_a_new_public_function_cannot_slip_in_unexplained():
    """The set is derived, so the gate is not "did somebody remember to list
    it" but "does every public function in the declared roots have a line"."""
    from stapel_tools.surface import scan_functions

    declared = {e["name"] for e in json.loads(COMMITTED.read_text())["surface"]}
    for module in ("validators.py", "watermarks.py", "fetch.py"):
        assert set(scan_functions(REPO / module)) <= declared
