"""ub-gate: pins, sandbox, refusal paths, summary, and (when installed) the gates themselves."""

import importlib.machinery
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
GATE = PLUGIN / "bin" / "ub-gate"


def load_gate():
    loader = importlib.machinery.SourceFileLoader("ub_gate", str(GATE))
    spec = importlib.util.spec_from_loader("ub_gate", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def gate(*args, cwd=None, env=None, check=False):
    full = dict(os.environ, **(env or {}))
    return subprocess.run([sys.executable, str(GATE), *args], cwd=cwd, env=full,
                          capture_output=True, text=True, check=check)


def sandbox_available():
    return platform.system() == "Linux" and load_gate().sandbox_works()


def installed():
    return (load_gate().ready_marker()).is_file()


# --- pins ---------------------------------------------------------------

def test_every_dependency_is_pinned_with_hashes():
    text = (PLUGIN / "lock" / "requirements.lock").read_text()
    reqs = re.findall(r"^([A-Za-z0-9_.-]+)==([^\s\\]+) \\\n((?:\s+--hash=sha256:[0-9a-f]{64}.*\n?)+)", text, re.M)
    names = {name for name, _, _ in reqs}
    assert {"tree-sitter", "tree-sitter-language-pack"} <= names
    unpinned = [line for line in text.splitlines() if re.match(r"^[A-Za-z]", line) and "==" not in line]
    assert not unpinned


def test_tools_pinned_to_full_commits_and_bundles_to_sha256():
    pins = json.loads((PLUGIN / "lock" / "pins.json").read_text())
    assert set(pins["tools"]) == {"crapper", "dryer", "mutator"}
    assert all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in pins["tools"].values())
    assert all(re.fullmatch(r"[0-9a-f]{64}", sha) for sha in pins["grammars"]["bundle_sha256"].values())


def test_lock_version_matches_grammar_pin():
    text = (PLUGIN / "lock" / "requirements.lock").read_text()
    pins = json.loads((PLUGIN / "lock" / "pins.json").read_text())
    assert f"tree-sitter-language-pack=={pins['grammars']['package_version']}" in text


# --- refusal paths ----------------------------------------------------------

def test_gates_refuse_until_installed(tmp_path):
    result = gate("crap", env={"UB_GATE_HOME": str(tmp_path)})
    assert result.returncode == 3 and "ub-gate install" in result.stderr


def test_unknown_command_fails():
    assert gate("frobnicate").returncode == 1


def test_no_namespace_stops_unless_network_allowed(monkeypatch):
    mod = load_gate()
    monkeypatch.setattr(mod, "sandbox_works", lambda: False)
    monkeypatch.delenv("UB_GATE_NETWORK", raising=False)
    with pytest.raises(SystemExit) as stop:
        mod.offline_argv(["true"])
    assert stop.value.code == 4
    monkeypatch.setenv("UB_GATE_NETWORK", "allow")
    assert mod.offline_argv(["true"]) == ["true"]


# --- sandbox ------------------------------------------------------------------

NETCHECK = """
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen()
socket.create_connection(s.getsockname()).close()
print("loopback ok")
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3)
    print("EGRESS OPEN")
except OSError as exc:
    print("egress blocked", exc.errno)
"""


@pytest.mark.skipif(not sandbox_available(), reason="needs Linux unprivileged user namespaces")
def test_sandbox_blocks_egress_keeps_loopback_and_drops_caps(tmp_path):
    script = tmp_path / "netcheck.py"
    script.write_text(NETCHECK)
    out = gate("sandbox", "--", sys.executable, "-I", str(script)).stdout
    assert "loopback ok" in out and "egress blocked" in out and "EGRESS OPEN" not in out
    caps = gate("sandbox", "--", "sh", "-c", "grep CapEff /proc/self/status; id -u").stdout
    assert "CapEff:\t0000000000000000" in caps
    assert caps.split()[-1] == str(os.getuid())


# --- summary --------------------------------------------------------------

def test_summary_digests_metrics(tmp_path):
    metrics = tmp_path / ".metrics"
    (metrics / "mutate").mkdir(parents=True)
    (metrics / "crap.edn").write_text(
        '{:entries [{:name "place" :namespace "demo.Board" :complexity 3 :coverage 75.0 :crap 3.1}\n'
        '           {:name "walk" :namespace "demo" :complexity 12 :coverage 0.0 :crap 156.0}]}\n')
    (metrics / "dry.edn").write_text('{:candidates [{:score 1.0 :language "go"} {:score 0.85 :language "go"}]}\n')
    (metrics / "mutate" / "demo.edn").write_text(
        '{:version 1 :forms [{:id "defn/place" :killed 3 :survived 1 :uncovered 2 :sites 6}]}\n')
    report = json.loads(gate("summary", cwd=tmp_path, check=True).stdout)
    assert report["crap"]["functions"] == 2 and report["crap"]["over_10"] == 1
    assert report["crap"]["worst"][0]["name"] == "walk"
    assert report["dry"] == {"pairs": 2, "at_1.0": 1}
    assert report["mutate"] == {"killed": 3, "survived": 1, "uncovered": 2, "score": 0.75}


# --- the gates, when the tools are installed ---------------------------------

needs_tools = pytest.mark.skipif(not (sandbox_available() and installed()),
                                 reason="run `ub-gate install` on a Linux host to exercise the gates")

HELPER = '''
def load_state(path):
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            return json.load(f)
    except Exception:
        return {}
'''

COPY = HELPER.replace("load_state", "read_board_state")


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


@needs_tools
def test_dry_against_repo_finds_a_copy_of_existing_code_that_changed_alone_misses(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "old.py").write_text("import json\n" + HELPER)
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "pkg" / "new.py").write_text("import json\n" + COPY)
    against = gate("dry", "--since", "HEAD", "--against-repo", cwd=tmp_path)
    alone = gate("dry", "--changed", cwd=tmp_path)
    assert against.returncode == 0, against.stderr
    assert "pkg/new.py" in against.stdout and "pkg/old.py" in against.stdout
    assert "DUPLICATE" not in alone.stdout


GO_ADULT = "package kat\n\nfunc Adult(age int) bool {\n\treturn age >= 18\n}\n"
GO_WEAK = 'package kat\n\nimport "testing"\n\nfunc TestAdult(t *testing.T) {\n\tif !Adult(30) || Adult(5) {\n\t\tt.Fatal("x")\n\t}\n}\n'
GO_STRONG = GO_WEAK.replace("Adult(5)", "Adult(5) || !Adult(18) || Adult(17)")


@needs_tools
@pytest.mark.skipif(shutil.which("go") is None, reason="needs go")
def test_mutate_survivor_on_weak_test_killed_by_boundary_test(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/kat\n\ngo 1.22\n")
    (tmp_path / "adult.go").write_text(GO_ADULT)
    (tmp_path / "adult_test.go").write_text(GO_WEAK)
    weak = gate("mutate", "adult.go", cwd=tmp_path)
    assert weak.returncode == 3 and "SURVIVED" in weak.stdout, weak.stdout + weak.stderr
    (tmp_path / "adult_test.go").write_text(GO_STRONG)
    strong = gate("mutate", "adult.go", cwd=tmp_path)
    assert strong.returncode == 0 and "SURVIVED" not in strong.stdout, strong.stdout + strong.stderr


@needs_tools
def test_a_parser_bundle_that_differs_from_the_pin_is_refused(monkeypatch):
    mod = load_gate()
    real = mod.venv()
    monkeypatch.setattr(mod, "venv", lambda: real)
    mod.PINS["grammars"]["bundle_sha256"][mod.platform_key()] = "0" * 64
    with pytest.raises(SystemExit) as stop:
        mod.verify_grammars()
    assert stop.value.code == 5
