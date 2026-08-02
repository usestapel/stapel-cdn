PYTHON ?= python3

.PHONY: migration-lint contract contract-check

# Expand/contract gate for Django migrations (release-management.md §3;
# stapel_tools.migration_lint). Requires stapel-tools importable (the
# workspace venv, or `pip install stapel-tools` once published).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict


# docs/llms.txt — the fifth contract artifact (badge-canon §3, stapel_tools.llms_txt),
# an agent-sized slice of docs/capabilities.json.
#
# These targets manage ONLY docs/llms.txt. docs/capabilities.json in this
# module is HAND-AUTHORED, not generated (see tests/test_capabilities_contract.py
# — cdn is the one module that still hand-authors it; a generator is tracked
# separately). Do not point `contract`/`contract-check` at it: regenerating it
# here would risk clobbering the hand-written `curated` prose it carries.
contract:
	$(PYTHON) -m stapel_tools.llms_txt .

contract-check:
	$(PYTHON) -m stapel_tools.llms_txt . --check
