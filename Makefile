PYTHON ?= python3

.PHONY: migration-lint contract contract-check

# Expand/contract gate for Django migrations (release-management.md §3;
# stapel_tools.migration_lint). Requires stapel-tools importable (the
# workspace venv, or `pip install stapel-tools` once published).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict


# docs/capabilities.json in this module is still HAND-AUTHORED for
# provides/axes/extension_points/requires (see tests/test_capabilities_contract.py
# — cdn is the one module with no schema/flows/errors emitter; a generator for
# the rest of the document is tracked separately). `--patch` regenerates ONLY
# the two derivable parts: module/version from pyproject, and the `surface`
# section — the symbols a product is meant to CALL (discoverability-design.md
# §1.2), derived by AST from docs/capabilities.meta.json's surface_roots. A
# selected export with no curated intent line fails this target naming it.
#
# Second: docs/llms.txt — the fifth contract artifact (badge-canon §3,
# stapel_tools.llms_txt), an agent-sized slice of the capabilities.json the
# step above produced.
contract:
	$(PYTHON) -m stapel_tools.surface . --patch
	$(PYTHON) -m stapel_tools.llms_txt .

contract-check:
	$(PYTHON) -m stapel_tools.surface . --patch --check
	$(PYTHON) -m stapel_tools.llms_txt . --check
