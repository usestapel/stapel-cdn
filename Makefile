PYTHON ?= python3

.PHONY: migration-lint contract contract-check

# Expand/contract gate for Django migrations (release-management.md §3;
# stapel_tools.migration_lint). Requires stapel-tools importable (the
# workspace venv, or `pip install stapel-tools` once published).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict


# stapel-cdn — contract emission + drift gate (contract-pipeline.md §2-3).
#
# This module emits its own contract triad (schema.json + flows.json +
# errors.json) from a single-module {cdn + core} Django instance mounted at
# the canonical /cdn/api/v1 prefix (see _codegen.py / _codegen_settings.py /
# codegen_urls.py) — the same mechanism stapel-search, stapel-chat and
# stapel-forms already use. Emission is pinned to Python 3.12:
# drf-spectacular renders component descriptions differently across minors,
# and a contract emitted on the wrong one produces false diffs forever.
#
# docs/capabilities.json in this module is still HAND-AUTHORED for
# provides/axes/extension_points/requires (see
# tests/test_capabilities_contract.py — a generator for the rest of that
# document is tracked separately). `--patch` regenerates ONLY the two
# derivable parts: module/version from pyproject, and the `surface` section
# — the symbols a product is meant to CALL (discoverability-design.md §1.2),
# derived by AST from docs/capabilities.meta.json's surface_roots. A
# selected export with no curated intent line fails this target naming it.
#
# Then: docs/llms.txt — the fifth contract artifact (badge-canon §3,
# stapel_tools.llms_txt), an agent-sized slice of docs/capabilities.json AND
# the triad above (llms_txt picks up schema/errors/flows automatically when
# present).
contract:
	$(PYTHON) -m stapel_cdn._codegen --out docs
	$(PYTHON) -m stapel_tools.surface . --patch
	$(PYTHON) -m stapel_tools.llms_txt .

# Drift gate: regenerate the triad into a temp dir and diff against the
# committed docs/*, then run the existing surface/llms.txt checks.
contract-check:
	@tmp=$$(mktemp -d); \
	$(PYTHON) -m stapel_cdn._codegen --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	rc=0; \
	for f in schema.json flows.json errors.json; do \
		if ! diff -q "docs/$$f" "$$tmp/$$f" >/dev/null 2>&1; then \
			echo "DRIFT: docs/$$f is stale — run 'make contract' and commit it"; \
			diff "docs/$$f" "$$tmp/$$f" | head -20; rc=1; \
		fi; \
	done; \
	rm -rf "$$tmp"; \
	$(PYTHON) -m stapel_tools.surface . --patch --check || rc=1; \
	$(PYTHON) -m stapel_tools.llms_txt . --check || rc=1; \
	if [ $$rc -eq 0 ]; then echo "contract-check: docs/{schema,flows,errors,capabilities,llms.txt} up to date"; fi; \
	exit $$rc
