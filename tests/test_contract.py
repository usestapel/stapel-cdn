"""The committed contract artifacts must describe the API that ships.

``make contract-check`` re-emits docs/{schema,flows,errors}.json via
``stapel_cdn._codegen`` and diffs them, but it needs the pinned Python 3.12
interpreter plus stapel-tools, so it is a dev-loop and release gate rather
than something a wider CI matrix can run unpinned. The tests below are the
part that runs everywhere: they read the COMMITTED artifacts and assert the
two properties a stale artifact silently breaks — every route this module
mounts is described, and every error key it can return is declared (A1,
darom-storefront-design.md §3.10 — the contract triad the react codegen
pipeline, ``gen:api``/``gen:errors``/``gen:manifest``, stands on).

``docs/capabilities.json`` in this module is hand-authored for
provides/axes/extension_points/requires (see
``test_capabilities_contract.py``) — this file does not touch that. It does
own ``docs/llms.txt``, the fifth contract artifact, which IS generated (from
capabilities.json + the triad) and can drift the moment either source
changes underneath it without a `make contract` re-run.
"""
import json
import re
from pathlib import Path

import pytest

try:
    import stapel_tools  # noqa: F401  (probe: the emitter must be importable)
except ImportError as exc:  # pragma: no cover - environment failure, not a branch
    # NOT pytest.importorskip. A drift gate that skips when its emitter is
    # missing reports `1 skipped`, exits 0, and disappears among a hundred
    # green tests — making "the tool is absent" indistinguishable from "there
    # is no drift". A gate that cannot run has FAILED; it has not passed.
    raise RuntimeError(
        "llms.txt drift gate cannot run: stapel-tools is not importable, and "
        "it carries the emitter this gate measures drift against. CI "
        "installs it; locally use the workspace venv or `pip install "
        "stapel-tools`. This is a hard failure on purpose — a skipped drift "
        "gate is silently no gate."
    ) from exc

from stapel_tools.llms_txt import load_inputs, render  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
COMMITTED = DOCS / "llms.txt"


@pytest.fixture(scope="module")
def errors_artifact():
    return {entry["code"]: entry for entry in json.loads((DOCS / "errors.json").read_text())}


@pytest.fixture(scope="module")
def schema_artifact():
    return json.loads((DOCS / "schema.json").read_text())


# ── errors.json ──────────────────────────────────────────────────────


def test_cdn_owned_keys_are_declared(errors_artifact):
    from stapel_cdn.errors import CDN_ERRORS

    for code, text in CDN_ERRORS.items():
        assert code in errors_artifact, f"{code} missing from docs/errors.json"
        assert errors_artifact[code]["owner"] == "stapel_cdn"
        assert errors_artifact[code]["en"] == text


# ── schema.json ──────────────────────────────────────────────────────


def test_every_mounted_route_is_described(schema_artifact):
    """A route added without regenerating the triad fails here.

    The gap this closes is not hypothetical: the pair reads ``schema.json``
    to generate its typed client, so an endpoint missing from the artifact is
    an endpoint the frontend cannot call.

    ``error-keys/`` is excluded on purpose: it is the stapel-translate
    collector's internal listing, not a product route (same exclusion
    stapel-forms' ``/error-keys/``-equivalent gate documents) — and
    drf-spectacular does not describe it either, confirming the exclusion
    rather than papering over a real gap.
    """
    from stapel_cdn import urls_v1

    described = set(schema_artifact["paths"])
    for pattern in urls_v1.urlpatterns:
        route = str(pattern.pattern)
        if route == "error-keys/":
            continue
        # `<str:image_type>` -> `{image_type}`, and drf-spectacular keeps the
        # trailing slash exactly as mounted.
        route = re.sub(r"<(?:[a-z_]+:)?([^>]+)>", r"{\1}", route)
        assert f"/cdn/api/v1/{route}" in described, (
            f"{route} is mounted but absent from docs/schema.json — "
            "run 'make contract' and commit the artifacts"
        )


def test_the_error_keys_route_stays_undescribed(schema_artifact):
    """Pin the exclusion above the other way: if spectacular ever starts

    describing it, the skip in the previous test would start silently hiding
    a real, describable route instead of a deliberately-excluded one.
    """
    assert "/cdn/api/v1/error-keys/" not in schema_artifact["paths"]


# ── docs/llms.txt — the fifth contract artifact -----------------------------


def test_llms_txt_committed():
    assert COMMITTED.is_file(), (
        "docs/llms.txt is missing — run `make contract` and commit it"
    )


def test_llms_txt_has_no_drift():
    rendered = render(load_inputs(REPO))
    assert COMMITTED.read_text() == rendered, (
        "docs/llms.txt is stale — run `make contract` and commit it"
    )


def test_llms_txt_emission_is_deterministic():
    """Two independent emissions are byte-identical (drift gate is meaningful)."""
    a = render(load_inputs(REPO))
    b = render(load_inputs(REPO))
    assert a == b
