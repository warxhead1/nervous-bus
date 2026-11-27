#!/usr/bin/env bash
# What a new user types on a fresh machine. Run by container_smoke.sh as a non-root user.
set -euo pipefail
cd ~/nbus
python3 -m venv ~/venv
. ~/venv/bin/activate
python -m pip install --quiet pytest
make install
export PATH="$HOME/.local/bin:$PATH"
make doctor
nervous publish agent.opencode.session '{"event":"started","session_id":"ses_smoke","ts":"2026-11-26T00:00:00Z"}'
grep -q ses_smoke ~/.cache/nervous-bus/debug.jsonl
transcripts sources
make hooks AGENT=opencode
test -f ~/.config/opencode/plugins/nervous-bus.js
make test
