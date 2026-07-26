"""`docs/capabilities.json` must not drift away from the package it describes.

Every other module in the fleet emits this file from a generator
(`_capabilities.py`, a shim over `stapel_tools.capabilities`, wired into
`make contract` and gated by `make contract-check`). stapel-cdn is the one
that still hand-authors it — so nothing regenerates the version field, and
nothing notices when it stops matching.

It had already stopped: 0.8.1 shipped with the file still claiming 0.8.0.
That is the same defect stapel-profiles 0.7.2 was released to fix, recurring
here for want of the gate rather than for want of the knowledge.

Until cdn grows a generator (which also has to preserve the hand-written
`curated` prose this file carries), this pins the part that provably drifts.
"""
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert match, "pyproject.toml has no version"
    return match.group(1)


def test_capabilities_version_matches_the_package():
    manifest = json.loads((REPO / "docs" / "capabilities.json").read_text("utf-8"))
    assert manifest["version"] == _pyproject_version(), (
        "docs/capabilities.json is hand-authored in this module — bump it in "
        "the same commit as pyproject.toml (0.8.1 shipped claiming 0.8.0)."
    )


def test_capabilities_names_this_module():
    manifest = json.loads((REPO / "docs" / "capabilities.json").read_text("utf-8"))
    assert manifest["module"] == "stapel-cdn"
