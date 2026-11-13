"""Verify-class commands: test, build, typecheck and lint runners.

A Python port of the claude-hook-fast `classifyVerify` tables, applied to one
simple command's words (not a whole line). Returns "" when the command is not
a verification. Stdlib only.
"""
from __future__ import annotations

import os
import re

ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
VERB_CHARS = re.compile(r"^[A-Za-z0-9._+][A-Za-z0-9._+-]{0,31}$")
SCRIPT_TEST_NAME = re.compile(
    r"^(?:run[-_]?)?(?:tests?|gate|verify)(?:[-_.][a-z0-9_-]*)?\.(?:sh|py)$|[-_](?:tests?|gate|verify)\.(?:sh|py)$",
    re.I)

CARGO_VALUED = {"-C", "--config", "-Z", "--manifest-path", "--color"}
GO_VALUED = {"-C"}
MAKE_VALUED = {"-C", "-f", "-I", "-o", "-W", "--directory", "--file", "-j", "-l"}
NPM_VALUED = {"--prefix", "-C", "--dir", "--filter", "-F", "-w", "--workspace", "--cwd"}
GRADLE_VALUED = {"-p", "-b", "-c", "--project-dir"}
PYTHON_VALUED = {"-W", "-X", "-c"}

CARGO_SUB = {"test": "test", "nextest": "test", "build": "build", "check": "build", "clippy": "lint"}
GO_SUB = {"test": "test", "build": "build", "vet": "lint"}
MAKE_SUB = {"test": "test", "tests": "test", "check": "test", "verify": "test", "ci": "test",
            "build": "build", "lint": "lint", "typecheck": "typecheck"}
MVN_SUB = {"test": "test", "verify": "test", "check": "test", "build": "build", "compile": "build",
           "package": "build", "assemble": "build"}
DOT_SUB = {"test": "test", "build": "build"}
PY_MODS = {"pytest": "test", "unittest": "test", "mypy": "typecheck", "pyright": "typecheck",
           "ruff": "lint", "flake8": "lint", "pylint": "lint", "compileall": "build", "py_compile": "build"}
BARE_VERB = {
    "pytest": "test", "py.test": "test", "vitest": "test", "jest": "test", "mocha": "test",
    "playwright": "test", "ctest": "test", "bats": "test", "nextest": "test",
    "tsc": "typecheck", "vue-tsc": "typecheck", "mypy": "typecheck", "pyright": "typecheck",
    "eslint": "lint", "biome": "lint", "ruff": "lint", "flake8": "lint", "pylint": "lint",
    "golangci-lint": "lint", "shellcheck": "lint", "ninja": "build",
}


class _T:
    """One word: .s is the unquoted value, .lit is False when it expands."""
    __slots__ = ("s", "lit")

    def __init__(self, s: str, lit: bool):
        self.s, self.lit = s, lit


def _first_positional(toks, valued) -> str:
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.s.startswith(("-", "+")):
            if valued and t.s in valued and i + 1 < len(toks):
                i += 1
            i += 1
            continue
        return t.s if t.lit else ""
    return ""


def _skip_flags(toks, valued):
    toks = list(toks)
    while toks and toks[0].s.startswith("-") and toks[0].s != "-":
        f = toks.pop(0).s
        if valued and f in valued and "=" not in f and toks:
            toks.pop(0)
    return toks


def _after(toks, s):
    for i, t in enumerate(toks):
        if t.lit and t.s == s:
            return toks[i + 1:]
    return []


def _verify_command(seg):
    i = 0
    while i < len(seg) and ENV_ASSIGN.match(seg[i].s):
        i += 1
    toks = seg[i:]
    for _ in range(6):
        if not toks:
            break
        if not toks[0].lit:
            return "", []
        v = os.path.basename(toks[0].s)
        if not VERB_CHARS.match(v):
            return "", []
        rest = toks[1:]
        if v == "env":
            while rest and (ENV_ASSIGN.match(rest[0].s) or rest[0].s.startswith("-")):
                rest = rest[1:]
        elif v in ("time", "nohup", "command", "exec", "builtin"):
            pass
        elif v in ("nice", "ionice", "sudo", "stdbuf", "chrt"):
            rest = _skip_flags(rest, {"-n", "-c", "-u", "-g", "-p"})
        elif v == "timeout":
            rest = _skip_flags(rest, {"-k", "-s", "--kill-after", "--signal"})
            if rest:
                rest = rest[1:]
        elif v in ("npx", "bunx", "pnpx"):
            rest = _skip_flags(rest, {"-p", "--package"})
        elif v in ("uv", "poetry", "pipenv", "pdm", "hatch", "rye"):
            if _first_positional(rest, None) != "run":
                return v, rest
            rest = _skip_flags(_after(rest, "run"), {"--with", "--python", "-p", "--project", "--directory"})
        elif v in ("pnpm", "yarn"):
            s = _first_positional(rest, NPM_VALUED)
            if s not in ("exec", "dlx"):
                return v, rest
            rest = _skip_flags(_after(rest, s), None)
        else:
            return v, rest
        toks = rest
    return "", []


def _script_class(name: str) -> str:
    n = name.lower()
    if not n:
        return ""
    if n.startswith(("test", "e2e")) or n in ("verify", "ci", "gate"):
        return "test"
    if n.startswith(("typecheck", "type-check")) or n in ("tsc", "check-types", "check"):
        return "typecheck"
    if n.startswith("lint"):
        return "lint"
    if n.startswith("build"):
        return "build"
    return ""


def _package_script(args) -> str:
    s = _first_positional(args, NPM_VALUED)
    if s in ("test", "t"):
        return "test"
    if s in ("run", "run-script"):
        s = _first_positional(_after(args, s), NPM_VALUED)
    return _script_class(s)


def _python(args) -> str:
    i = 0
    while i < len(args):
        a = args[i]
        if not a.lit:
            return ""
        if a.s == "-m" and i + 1 < len(args) and args[i + 1].lit:
            return PY_MODS.get(args[i + 1].s, "")
        if a.s in PYTHON_VALUED:
            i += 2
            continue
        if a.s.startswith("-"):
            i += 1
            continue
        b = os.path.basename(a.s)
        return "test" if (SCRIPT_TEST_NAME.match(b) or b.startswith("test_")) else ""
    return ""


def classify(words) -> str:
    """Verify class of one simple command; `words` have .value and .dynamic."""
    seg = [_T(w.value, not w.dynamic) for w in words]
    verb, args = _verify_command(seg)
    if not verb:
        return ""

    def sub(valued):
        return _first_positional(args, valued)

    if verb == "cargo":
        return CARGO_SUB.get(sub(CARGO_VALUED), "")
    if verb == "go":
        return GO_SUB.get(sub(GO_VALUED), "")
    if verb in ("make", "just"):
        return MAKE_SUB.get(sub(MAKE_VALUED), "")
    if verb in ("mvn", "gradle", "gradlew", "mvnw"):
        return MVN_SUB.get(sub(GRADLE_VALUED), "")
    if verb in ("dotnet", "swift", "deno"):
        s = sub(None)
        if verb == "deno" and s == "check":
            return "typecheck"
        return DOT_SUB.get(s, "")
    if verb == "xcodebuild":
        return next((DOT_SUB[a.s] for a in args if a.lit and a.s in DOT_SUB), "")
    if verb == "cmake":
        return "build" if any(a.lit and a.s == "--build" for a in args) else ""
    if verb in ("npm", "pnpm", "yarn", "bun"):
        return _package_script(args)
    if verb in ("bash", "sh", "zsh"):
        s = sub(None)
        if s and SCRIPT_TEST_NAME.match(os.path.basename(s)):
            return "test"
    if verb.startswith("python"):
        return _python(args)
    if verb in BARE_VERB:
        return BARE_VERB[verb]
    if SCRIPT_TEST_NAME.match(verb):
        return "test"
    return ""
