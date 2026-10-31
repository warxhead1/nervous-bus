# Thin wrappers only. All logic lives in tools/setup.py (works without make: `py tools/setup.py ...`).
PYTHON ?= python3
SETUP  := $(PYTHON) tools/setup.py

.DEFAULT_GOAL := help
.PHONY: help doctor install uninstall hooks test lint schemas channels plugin

help: ## Show this help
	@awk 'BEGIN{FS=":.*## "} /^[a-zA-Z0-9_%-]+:.*## /{printf "  %-16s %s\n",$$1,$$2}' $(MAKEFILE_LIST)

doctor: ## Check python, bash, redis, deps, coding agents, installed CLIs
	@$(SETUP) doctor

install: ## Link CLIs into ~/.local/bin (BIN_DIR=... to override; DRY=1 to preview)
	@$(SETUP) install $(if $(BIN_DIR),--bin-dir $(BIN_DIR)) $(if $(DRY),--dry-run)

uninstall: ## Remove CLI links that point into this checkout
	@$(SETUP) uninstall $(if $(BIN_DIR),--bin-dir $(BIN_DIR)) $(if $(DRY),--dry-run)

hooks: ## Wire live hooks into Claude Code (argshape); AGENT=... DRY=1
	@$(SETUP) hooks $(if $(AGENT),--agent $(AGENT)) $(if $(DRY),--dry-run)

test: ## Run every test suite, each in its own process
	@$(SETUP) test

test-%: ## Run one suite (make test-transcripts; see: python3 tools/setup.py test --list)
	@$(SETUP) test $*

lint: ## shellcheck sdk/shell/* and pyflakes (each skipped if not installed)
	@if command -v shellcheck >/dev/null 2>&1; then \
	  for f in sdk/shell/nervous sdk/shell/nbd sdk/shell/probe; do [ -f $$f ] && shellcheck -S error $$f; done; \
	else echo "shellcheck not installed: skipped"; fi
	@if $(PYTHON) -m pyflakes --version >/dev/null 2>&1; then \
	  $(PYTHON) -m pyflakes tools/setup.py adapters/transcripts; \
	else echo "pyflakes not installed: skipped"; fi

schemas: ## Validate schemas/*.json as Draft 2020-12 (same check as ci-schema-lint)
	@$(SETUP) schemas

channels: ## Regenerate schemas/CHANNELS.md
	@$(PYTHON) tools/gen_channels_md.py

plugin: ## Build the zellij WASM plugin (needs cargo + wasm32-wasi target)
	cd plugin && cargo build --release --target wasm32-wasi
