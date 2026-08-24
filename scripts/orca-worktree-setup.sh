#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

command -v python3 >/dev/null 2>&1 || {
  echo "Python 3 is required by the nervous-bus shell SDK." >&2
  exit 1
}

chmod +x sdk/shell/nervous
echo "nervous-bus core is ready; optional submodules and Redis were not changed."
