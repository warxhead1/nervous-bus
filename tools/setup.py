#!/usr/bin/env python3
"""nervous-bus portable setup: doctor / install / uninstall / hooks / test.

Stdlib only, Python >= 3.9, Linux + macOS + Windows.  No make required:

    python3 tools/setup.py doctor [--json] [--strict]
    python3 tools/setup.py install [--bin-dir DIR] [--dry-run]
    python3 tools/setup.py uninstall [--bin-dir DIR] [--dry-run]
    python3 tools/setup.py hooks [--agent claude] [--dry-run]
    python3 tools/setup.py test [--list] [SUITE ...]
    python3 tools/setup.py schemas

On Windows use `py tools/setup.py ...`.  The Makefile is a thin wrapper over this file.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
SHIM_MARK = "nervous-bus shim"

# name -> (path relative to repo, kind)
CLIS = {
    "argshape": ("adapters/argshape/argshape.py", "python"),
    "needs-you": ("adapters/needs-you/needs_you.py", "python"),
    "transcripts": ("adapters/transcripts/transcripts.py", "python"),
    "nervous": ("sdk/shell/nervous", "bash"),
    "nbd": ("sdk/shell/nbd", "bash"),
    "probe": ("sdk/shell/probe", "bash"),
    "deer-bus-listen": ("sdk/python/nbus.py", "bash"),  # bash shim, see nbus.py
}
SKIP_SUITE_PARTS = {"vendor", "autobench", "node_modules", ".git", "__pycache__"}


# ---------------------------------------------------------------- helpers

def is_windows(platform: str | None = None) -> bool:
    return (platform or sys.platform) == "win32"


def find_bash() -> str | None:
    return shutil.which("bash")


def default_bin_dir(platform: str | None = None) -> Path:
    if is_windows(platform):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "nervous-bus" / "bin"
    return Path.home() / ".local" / "bin"


def on_path(d: Path) -> bool:
    want = os.path.normcase(os.path.realpath(str(d)))
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if p and os.path.normcase(os.path.realpath(os.path.expanduser(p))) == want:
            return True
    return False


def path_hint(d: Path, platform: str | None = None) -> str:
    if is_windows(platform):
        return f'setx PATH "%PATH%;{d}"'
    return f'export PATH="{d}:$PATH"'


def script_path(name: str, repo: Path = REPO) -> Path:
    return repo / CLIS[name][0]


def cli_names(bash: bool, platform: str | None = None) -> list[str]:
    """CLIs installable here.  Bash CLIs only where bash exists."""
    out = []
    for n, (_, kind) in CLIS.items():
        if kind == "bash" and not bash:
            continue
        out.append(n)
    return out


# ---------------------------------------------------------------- link / shim ownership

def classify(path: Path, name: str, repo: Path = REPO) -> tuple[str, str]:
    """State of `path` as a CLI install slot.

    ('missing'|'ours'|'other-checkout'|'foreign', detail).  'ours' points into `repo`;
    'other-checkout' is a link/shim to the same script in another nervous-bus checkout;
    'foreign' is anything else and must never be overwritten.
    """
    rel = CLIS[name][0]
    if path.is_symlink():
        target = os.path.realpath(path)
        if os.path.normcase(target) == os.path.normcase(str(script_path(name, repo).resolve())):
            return "ours", target
        if target.replace("\\", "/").endswith("/" + rel):
            return "other-checkout", target
        return "foreign", target
    if path.exists():
        if path.is_file() and path.stat().st_size < 4096:
            try:
                text = path.read_text(errors="replace")
            except OSError:
                return "foreign", "unreadable"
            if SHIM_MARK in text:
                tgt = _shim_target(text)
                if tgt and os.path.normcase(os.path.realpath(tgt)) == os.path.normcase(
                        str(script_path(name, repo).resolve())):
                    return "ours", tgt
                return "other-checkout", tgt or ""
        return "foreign", "regular file"
    return "missing", ""


def _shim_target(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("@rem target="):
            return line[len("@rem target="):].strip()
    return ""


def shim_text(name: str, repo: Path = REPO, platform: str | None = None, bash: str | None = None) -> str:
    """Windows .cmd shim body for CLI `name`."""
    script = script_path(name, repo)
    kind = CLIS[name][1]
    runner = f'"{bash or "bash"}"' if kind == "bash" else "py -3"
    return (f"@echo off\r\n@rem {SHIM_MARK}\r\n@rem target={script}\r\n"
            f'{runner} "{script}" %*\r\n')


def shim_path(bin_dir: Path, name: str) -> Path:
    return bin_dir / f"{name}.cmd"


def install_one(name: str, bin_dir: Path, *, windows: bool, repo: Path = REPO,
                dry_run: bool = False, bash: str | None = None) -> dict:
    script = script_path(name, repo)
    dest = shim_path(bin_dir, name) if windows else bin_dir / name
    state, detail = classify(dest, name, repo)
    res = {"name": name, "dest": str(dest), "script": str(script), "state": state}
    if not script.exists():
        res["action"] = "skipped: script missing"
        return res
    if state == "foreign":
        res["action"] = f"refused: {dest} exists and is not a nervous-bus link ({detail})"
        return res
    if state == "ours":
        res["action"] = "unchanged"
        return res
    res["action"] = "would link" if dry_run else ("relinked" if state == "other-checkout" else "linked")
    if state == "other-checkout" and dry_run:
        res["action"] = "would relink (from another checkout)"
    if dry_run:
        return res
    bin_dir.mkdir(parents=True, exist_ok=True)
    if state == "other-checkout":
        dest.unlink()
    if windows:
        with open(dest, "w", newline="") as f:
            f.write(shim_text(name, repo, bash=bash))
    else:
        dest.symlink_to(script)
        try:
            if not os.access(script, os.X_OK):
                script.chmod(script.stat().st_mode | 0o111)
        except OSError:
            pass
    return res


def uninstall_one(name: str, bin_dir: Path, *, windows: bool, repo: Path = REPO,
                  dry_run: bool = False) -> dict:
    dest = shim_path(bin_dir, name) if windows else bin_dir / name
    state, detail = classify(dest, name, repo)
    res = {"name": name, "dest": str(dest), "state": state}
    if state == "ours":
        res["action"] = "would remove" if dry_run else "removed"
        if not dry_run:
            dest.unlink()
    elif state == "missing":
        res["action"] = "absent"
    else:
        res["action"] = f"kept: {state} ({detail})"
    return res


# ---------------------------------------------------------------- doctor

def redis_target() -> tuple[str, int, str]:
    url = os.environ.get("NBUS_REDIS_URL") or "redis://127.0.0.1:6379"
    u = urlparse(url if "://" in url else "redis://" + url)
    return u.hostname or "127.0.0.1", u.port or 6379, url


def redis_ping(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"PING\r\n")
            return s.recv(64).startswith((b"+PONG", b"-NOAUTH"))
    except OSError:
        return False


def py_deps() -> dict:
    need = ["redis", "jsonschema"] + (["tomli"] if sys.version_info < (3, 11) else [])
    out = {}
    for m in need:
        try:
            importlib.import_module(m)
            out[m] = True
        except Exception:
            out[m] = False
    return out


def agent_report() -> dict:
    sys.path.insert(0, str(REPO / "adapters" / "transcripts"))
    try:
        from sources import SOURCES  # type: ignore
    except Exception as e:  # noqa: BLE001
        return {"_error": f"cannot import adapters/transcripts: {e}"}
    out = {}
    for name, src in SOURCES.items():
        try:
            roots = [{"path": r, "exists": os.path.isdir(r)} for r in src.roots()]
        except Exception as e:  # noqa: BLE001
            roots = [{"path": f"error: {e}", "exists": False}]
        out[name] = {"label": src.label or name, "roots": roots,
                     "present": any(r["exists"] for r in roots),
                     "live_hooks": src.live_hooks}
    return out


def doctor_data(bin_dir: Path | None = None) -> dict:
    win = is_windows()
    bin_dir = bin_dir or default_bin_dir()
    bash = find_bash()
    host, port, url = redis_target()
    deps = py_deps()
    clis = {}
    for name in CLIS:
        dest = shim_path(bin_dir, name) if win else bin_dir / name
        state, detail = classify(dest, name)
        clis[name] = {"path": str(dest), "state": state, "target": detail,
                      "needs_bash": CLIS[name][1] == "bash"}
    problems, warnings = [], []
    pyok = sys.version_info >= (3, 9)
    if not pyok:
        problems.append("python >= 3.9 required")
    missing = [d for d, ok in deps.items() if not ok]
    if missing:
        problems.append("missing python deps: " + ", ".join(missing))
    if not bash:
        msg = "bash not found (shell SDK needs it)" + (": use WSL or Git Bash" if win else "")
        (warnings if win else problems).append(msg)
    redis_ok = redis_ping(host, port)
    if not redis_ok:
        warnings.append(f"redis not reachable at {host}:{port} (optional until you run redis-mirror)")
    if not on_path(bin_dir):
        warnings.append(f"{bin_dir} is not on PATH: {path_hint(bin_dir)}")
    return {
        "platform": sys.platform,
        "repo": str(REPO),
        "python": {"version": ".".join(map(str, sys.version_info[:3])), "executable": sys.executable, "ok": pyok},
        "bash": {"found": bool(bash), "path": bash},
        "redis": {"url": url, "host": host, "port": port, "reachable": redis_ok},
        "deps": deps,
        "pip_fix": (f"{sys.executable} -m pip install " + " ".join(missing)) if missing else "",
        "agents": agent_report(),
        "bin_dir": {"path": str(bin_dir), "on_path": on_path(bin_dir)},
        "clis": clis,
        "problems": problems,
        "warnings": warnings,
        "ok": not problems,
    }


def cmd_doctor(args) -> int:
    d = doctor_data(Path(args.bin_dir) if args.bin_dir else None)
    if args.json:
        print(json.dumps(d, indent=2))
    else:
        mark = lambda ok: "ok " if ok else "-- "  # noqa: E731
        print(f"nervous-bus doctor  ({d['platform']}, repo {d['repo']})")
        print(f"{mark(d['python']['ok'])}python {d['python']['version']}")
        print(f"{mark(d['bash']['found'])}bash {d['bash']['path'] or 'not found'}")
        print(f"{mark(d['redis']['reachable'])}redis {d['redis']['host']}:{d['redis']['port']}")
        for m, ok in d["deps"].items():
            print(f"{mark(ok)}python module {m}")
        if d["pip_fix"]:
            print(f"   fix: {d['pip_fix']}")
        print("coding agents (adapters/transcripts):")
        for n, a in d["agents"].items():
            if n == "_error":
                print(f"   {a}")
                continue
            print(f"{mark(a['present'])}{n} ({a['label']})")
            for r in a["roots"]:
                print(f"     {'found  ' if r['exists'] else 'missing'} {r['path']}")
        print(f"CLIs in {d['bin_dir']['path']}{'' if d['bin_dir']['on_path'] else '  (NOT on PATH)'}:")
        for n, c in d["clis"].items():
            what = {"ours": "-> this checkout", "missing": "not installed",
                    "other-checkout": f"-> another checkout ({c['target']})",
                    "foreign": f"foreign file, will not overwrite ({c['target']})"}[c["state"]]
            print(f"{mark(c['state'] == 'ours')}{n}: {what}")
        for w in d["warnings"]:
            print(f"warn: {w}")
        for p in d["problems"]:
            print(f"PROBLEM: {p}")
    return 1 if (args.strict and not d["ok"]) else 0


# ---------------------------------------------------------------- install / uninstall

def cmd_install(args) -> int:
    win = is_windows()
    bin_dir = Path(args.bin_dir).expanduser() if args.bin_dir else default_bin_dir()
    bash = find_bash()
    results = [install_one(n, bin_dir, windows=win, dry_run=args.dry_run, bash=bash)
               for n in cli_names(bool(bash))]
    if not bash:
        skipped = [n for n, (_, k) in CLIS.items() if k == "bash"]
        print(f"bash not found: skipped shell CLIs ({', '.join(skipped)})"
              + ("; use WSL or Git Bash" if win else ""))
    for r in results:
        print(f"{r['action']:<28} {r['name']:<16} {r['dest']}")
    if not on_path(bin_dir):
        print(f"\n{bin_dir} is not on PATH.  Add it:\n  {path_hint(bin_dir)}")
    home = os.environ.get("NERVOUS_HOME") or str(Path.home() / ".config" / "nervous-bus")
    if bash and not win:
        if args.dry_run:
            print(f"would run: bash sdk/shell/nervous setup  (creates {home})")
        else:
            subprocess.run([bash, str(script_path("nervous")), "setup"], check=False,
                           stdout=subprocess.DEVNULL)
            print(f"config dir ready: {home}")
    else:
        if not args.dry_run:
            for sub in ("schemas", "adapters"):
                Path(home, sub).mkdir(parents=True, exist_ok=True)
            print(f"config dir ready: {home}")
        else:
            print(f"would create config dir: {home}")
    return 1 if any(r["action"].startswith("refused") for r in results) else 0


def cmd_uninstall(args) -> int:
    win = is_windows()
    bin_dir = Path(args.bin_dir).expanduser() if args.bin_dir else default_bin_dir()
    for n in CLIS:
        r = uninstall_one(n, bin_dir, windows=win, dry_run=args.dry_run)
        print(f"{r['action']:<28} {r['name']:<16} {r['dest']}")
    return 0


# ---------------------------------------------------------------- hooks

def cmd_hooks(args) -> int:
    report = agent_report()
    if args.agent == "claude":
        cmd = [sys.executable, str(script_path("argshape")), "install"]
        if args.dry_run:
            print("would run: " + " ".join(cmd))
            return 0
        if is_windows():
            print("note: argshape install wires ~/.local/bin/argshape into Claude settings; "
                  "on Windows that path does not exist yet (unsupported, see issue notes).")
        return subprocess.run(cmd).returncode
    print(f"hooks for '{args.agent}' are not supported yet.")
    for n, a in report.items():
        if n == "_error":
            print(a)
        else:
            print(f"  {n}: {a['live_hooks'] or 'no live hook mechanism recorded'}")
    return 0


# ---------------------------------------------------------------- tests

def discover_suites(repo: Path = REPO) -> dict:
    """name -> directory.  One suite per adapters/<x>, plus tests/ and tools/."""
    def has_tests(d: Path) -> bool:
        for root, dirs, files in os.walk(d):
            dirs[:] = [x for x in dirs if x not in SKIP_SUITE_PARTS]
            if any(f.startswith("test_") and f.endswith(".py") for f in files):
                return True
        return False

    suites = {}
    ad = repo / "adapters"
    if ad.is_dir():
        for d in sorted(ad.iterdir()):
            if d.is_dir() and d.name not in SKIP_SUITE_PARTS and has_tests(d):
                suites[d.name] = d
    for top in ("tests", "tools"):
        d = repo / top
        if d.is_dir() and has_tests(d):
            suites[top] = d
    return suites


def cmd_test(args) -> int:
    suites = discover_suites()
    if args.list:
        for n, d in suites.items():
            print(f"{n}\t{d.relative_to(REPO).as_posix()}")
        return 0
    want = args.suite or list(suites)
    unknown = [w for w in want if w not in suites]
    if unknown:
        print(f"unknown suite(s): {', '.join(unknown)}; try --list", file=sys.stderr)
        return 2
    results = {}
    for n in want:
        print(f"=== {n}", flush=True)
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(suites[n])],
                           cwd=REPO)
        results[n] = p.returncode
    print("\nsummary:")
    for n, rc in results.items():
        label = "pass" if rc == 0 else ("no tests collected" if rc == 5 else f"FAIL (rc {rc})")
        print(f"  {n:<22} {label}")
    return 1 if any(rc not in (0, 5) for rc in results.values()) else 0


# ---------------------------------------------------------------- schemas

def cmd_schemas(args) -> int:
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        print("jsonschema not installed: " + sys.executable + " -m pip install jsonschema", file=sys.stderr)
        return 1
    bad, n = [], 0
    for f in sorted((REPO / "schemas").glob("*.json")):
        if f.name.startswith("_"):
            continue
        n += 1
        try:
            Draft202012Validator.check_schema(json.loads(f.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            bad.append(f.name)
            print(f"INVALID SCHEMA: {f.name}: {str(e).splitlines()[0]}")
    print(f"{n - len(bad)}/{n} schemas valid (Draft 2020-12)")
    return 1 if bad else 0


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="setup.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("doctor", help="check this machine")
    s.add_argument("--json", action="store_true")
    s.add_argument("--strict", action="store_true", help="exit 1 on missing required pieces")
    s.add_argument("--bin-dir")
    s.set_defaults(fn=cmd_doctor)
    for name, fn in (("install", cmd_install), ("uninstall", cmd_uninstall)):
        s = sub.add_parser(name, help=f"{name} CLIs in a user bin dir")
        s.add_argument("--bin-dir")
        s.add_argument("--dry-run", action="store_true")
        s.set_defaults(fn=fn)
    s = sub.add_parser("hooks", help="wire live hooks into a coding agent")
    s.add_argument("--agent", default="claude")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_hooks)
    s = sub.add_parser("schemas", help="validate schemas/*.json as Draft 2020-12")
    s.set_defaults(fn=cmd_schemas)
    s = sub.add_parser("test", help="run each test suite in its own process")
    s.add_argument("--list", action="store_true")
    s.add_argument("suite", nargs="*")
    s.set_defaults(fn=cmd_test)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
