#!/usr/bin/env bash
# Clean-container install check: a fresh Linux image, a non-root user, a copy of this checkout,
# then the documented one-command install and the portable test suites.
#
#   tools/container_smoke.sh [image ...]      default: debian:bookworm ubuntu:22.04
#
# Needs docker and network (apt, pip).  Exit 0 only when every image passes.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
images=("$@")
[ ${#images[@]} -gt 0 ] || images=(debian:bookworm ubuntu:22.04)

root_steps='
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq python3 python3-venv python3-pip make bash jq nodejs git >/dev/null
useradd -m tester
cp -r /src /home/tester/nbus
chown -R tester:tester /home/tester/nbus
exec su tester -c "bash /home/tester/nbus/tools/container_user_steps.sh"
'

fail=0
for img in "${images[@]}"; do
  echo "=== $img"
  if docker run --rm -v "$repo:/src:ro" "$img" bash -c "$root_steps"; then
    echo "PASS $img"
  else
    echo "FAIL $img"
    fail=1
  fi
done
exit $fail
