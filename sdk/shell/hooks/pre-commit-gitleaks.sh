#!/usr/bin/env bash
# pre-commit hook — runs gitleaks over staged changes.
#
# Install:
#   cp sdk/shell/hooks/pre-commit-gitleaks.sh .git/hooks/pre-commit
#   chmod +x .git/hooks/pre-commit
#
# Or via nervous setup (run `nervous setup --install-hooks`).
# Requires gitleaks on PATH: https://github.com/gitleaks/gitleaks

set -euo pipefail

if ! command -v gitleaks >/dev/null 2>&1; then
    echo "[gitleaks] not installed — skipping pre-commit scan" >&2
    echo "[gitleaks] install: https://github.com/gitleaks/gitleaks#installing" >&2
    exit 0
fi

gitleaks protect --staged \
    --config "$(git rev-parse --show-toplevel)/.gitleaks.toml" \
    --redact \
    --verbose
