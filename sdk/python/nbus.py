#!/usr/bin/env bash
# nbus — back-compat shim. The Python listener that lived here was retired;
# its functionality moved into deer-flow's `deer obs bus` subcommand.
#
# This file remains as a bash shim because `~/.local/bin/deer-bus-listen`
# symlinks here and we don't want to invalidate every consumer's PATH entry.
# The .py extension is deliberate — it preserves the symlink path; the
# shebang dispatches to bash.
#
# Don't add new Python code here. New Rust producers/consumers should land
# under sdk/rust/. Quick scripts should call `deer obs bus` directly or
# shell out to `nervous publish`.
echo "[deprecated] use 'deer obs bus' instead — see 'deer obs bus --help'" >&2
exec deer obs bus "$@"
