"""Regression tests for the schema-VALIDATION degradation signal (nervous-bus-gzsv).

The `nervous` shell SDK validates publishes against `schemas/<channel>.json`
using the `jsonschema` python module. On a host whose resolved python3 lacks
that module (the systemd-service `/usr/bin/python3` case), validation is
SILENTLY skipped — and historically `nervous publish` still exited 0, so a
Rust/Go caller could not distinguish "validated + delivered" from "wrote
debug.jsonl with NO schema enforcement".

These tests stub a python interpreter that lacks `jsonschema` (via a wrapper
that installs an import-blocking meta-path finder) and assert that publish
SURFACES the degradation:

  * a machine-readable marker on stdout
    (`delivery=file-only validation=skipped reason=jsonschema_module_missing`);
  * a non-zero exit when the caller opts into NERVOUS_STRICT_VALIDATION=1.

Runs the real shell script with Redis + zellij disabled and the debug log
redirected to a tmp file, so it needs no live services.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
NERVOUS = REPO / "sdk" / "shell" / "nervous"

# A channel that HAS a repo schema, so the validation block is actually entered
# (publish only validates when a schema file exists for the channel).
CHANNEL = "agent.message.v1"


def _interpreter_has_jsonschema(py: str) -> bool:
    return subprocess.run([py, "-c", "import jsonschema"], capture_output=True).returncode == 0


def _pick_validated_channel() -> str:
    """Return a channel whose schema exists in the repo, so validation runs."""
    schema_dir = REPO / "schemas"
    candidate = schema_dir / f"{CHANNEL}.json"
    if candidate.exists():
        return CHANNEL
    # Fallback: any schema file in the repo.
    for f in sorted(schema_dir.glob("*.json")):
        return f.stem
    pytest.skip("no repo schemas available to drive the validation path")


@pytest.fixture()
def no_jsonschema_python(tmp_path: Path) -> Path:
    """A python3 wrapper whose `import jsonschema` raises ImportError.

    Implemented as a real interpreter invocation with a `-c` preamble that
    installs a meta-path finder blocking the `jsonschema` module, then re-exec's
    the requested program. We can't just hide an installed module via PYTHONPATH
    (a real jsonschema on sys.path still imports), so we block it explicitly.
    """
    blocker = tmp_path / "block_jsonschema.py"
    blocker.write_text(
        "import sys\n"
        "class _Block:\n"
        "    def find_module(self, name, path=None):\n"
        "        if name == 'jsonschema' or name.startswith('jsonschema.'):\n"
        "            return self\n"
        "        return None\n"
        "    def load_module(self, name):\n"
        "        raise ImportError('blocked for test: ' + name)\n"
        "    # PEP 451 hook\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'jsonschema' or name.startswith('jsonschema.'):\n"
        "            raise ImportError('blocked for test: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
    )

    wrapper = tmp_path / "python3"
    real = sys.executable
    # The wrapper forwards all args to the real python but with our blocker
    # injected via PYTHONSTARTUP-style preamble. `-c` programs are the only thing
    # nervous invokes through $NERVOUS_PYTHON for validation, so we special-case
    # `-c <code>` and run: real -c "<blocker>; <code>".
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        f'REAL="{real}"\n'
        f'BLOCK="{blocker}"\n'
        'if [[ "$1" == "-c" ]]; then\n'
        '    shift\n'
        '    code="$1"; shift\n'
        '    exec "$REAL" -c "$(cat "$BLOCK")"$\'\\n\'"$code" "$@"\n'
        'fi\n'
        'exec "$REAL" "$@"\n'
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return wrapper


def _publish(channel: str, debug_log: Path, *, strict: bool, py: Path) -> subprocess.CompletedProcess:
    env = {
        "PATH": "/usr/bin:/bin",
        "NERVOUS_NO_REDIS": "1",
        "NERVOUS_NO_ZELLIJ": "1",
        "NERVOUS_SKIP_ALLOWLIST": "1",
        "NERVOUS_DEBUG_LOG": str(debug_log),
        "HOME": str(debug_log.parent),
        "NERVOUS_PYTHON": str(py),
    }
    if strict:
        env["NERVOUS_STRICT_VALIDATION"] = "1"
    return subprocess.run(
        [str(NERVOUS), "publish", channel, json.dumps({"hello": "world"})],
        text=True,
        capture_output=True,
        env=env,
    )


@pytest.fixture()
def debug_log(tmp_path: Path) -> Path:
    return tmp_path / "debug.jsonl"


def test_validator_python_lacks_jsonschema(no_jsonschema_python: Path):
    """Sanity: the stubbed python really cannot import jsonschema."""
    proc = subprocess.run(
        [str(no_jsonschema_python), "-c", "import jsonschema; print('LOADED')"],
        text=True,
        capture_output=True,
    )
    assert proc.returncode != 0, proc.stdout
    assert "LOADED" not in proc.stdout


def test_missing_jsonschema_surfaces_stdout_marker(debug_log: Path, no_jsonschema_python: Path):
    channel = _pick_validated_channel()
    proc = _publish(channel, debug_log, strict=False, py=no_jsonschema_python)
    # Non-strict: fail-soft, exit 0, but the degradation MUST be visible on stdout.
    assert proc.returncode == 0, proc.stderr
    assert "delivery=file-only" in proc.stdout, proc.stdout
    assert "validation=skipped" in proc.stdout, proc.stdout
    assert "jsonschema_module_missing" in proc.stdout, proc.stdout
    # Event is still durable in debug.jsonl.
    lines = [ln for ln in debug_log.read_text().splitlines() if ln.strip()]
    assert any(json.loads(ln).get("type") == channel for ln in lines)


def test_missing_jsonschema_strict_mode_nonzero_exit(debug_log: Path, no_jsonschema_python: Path):
    channel = _pick_validated_channel()
    proc = _publish(channel, debug_log, strict=True, py=no_jsonschema_python)
    # Strict: a Rust/Go caller that needs guaranteed validation gets a non-zero exit.
    assert proc.returncode != 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "delivery=file-only" in proc.stdout, proc.stdout


def test_present_jsonschema_no_degradation_marker(debug_log: Path):
    """Control: with jsonschema present, no degradation marker.

    Pins NERVOUS_PYTHON to the test interpreter, which has jsonschema (the bare
    /usr/bin/python3 on some hosts does not — that's the very degradation this
    bead surfaces, so it would falsely trip this control if relied upon).
    """
    if not _interpreter_has_jsonschema(sys.executable):
        pytest.skip("test interpreter lacks jsonschema; cannot drive the present-case control")
    channel = _pick_validated_channel()
    proc = subprocess.run(
        [str(NERVOUS), "publish", channel, json.dumps({"hello": "world"})],
        text=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "NERVOUS_NO_REDIS": "1",
            "NERVOUS_NO_ZELLIJ": "1",
            "NERVOUS_SKIP_ALLOWLIST": "1",
            "NERVOUS_DEBUG_LOG": str(debug_log),
            "HOME": str(debug_log.parent),
            "NERVOUS_PYTHON": sys.executable,
        },
    )
    # May be exit 0 (valid) or 2 (schema violation), but never the skip marker.
    assert "validation=skipped" not in proc.stdout, proc.stdout
