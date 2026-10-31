"""Learn a binary's option surface from its own --help.

Runs only off the hot path (after a usage failure, or `argshape learn`), at
most once per binary build: the result is keyed by the binary's identity
(device, inode, mtime, size), so an upgrade re-learns and a rebuild of the
same version does too.

Safety before parsing: running `X --help` executes X. A program that treats
`--help` as data (a naive script) would run its real action. So help is only
requested from programs that already proved they parse arguments (they just
printed a usage error) or that are ELF binaries / scripts that visibly use an
arg parser, never from a deny-list of destructive commands, always with
stdin closed, pagers disabled, no TTY, and a hard timeout.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field

NEVER_RUN = {
    "rm", "rmdir", "dd", "mkfs", "shred", "shutdown", "reboot", "poweroff",
    "halt", "kill", "pkill", "killall", "truncate", "wipefs", "fdisk",
    "parted", "sudo", "su", "doas", "passwd", "crontab", "at", "init",
    "telinit", "mount", "umount", "swapoff",
}
SCRIPT_PARSER_HINT = re.compile(
    rb"argparse|click|typer|docopt|getopts|getopt|optparse|commander|yargs|"
    rb"clap|cobra|flag\.Parse|--help|usage\(\)|Usage:", re.I)

# One option-spec line: leading indent, then options separated by ", " or
# " | ", then (two+ spaces or end) the description.
OPT_LINE = re.compile(r"^\s{0,12}(-{1,2}[\[A-Za-z0-9?].*?)(?:\s{2,}|\t|$)")
OPT_TOKEN = re.compile(
    r"(?<![\w-])(-{1,2}(?:\[no-\])?[A-Za-z0-9?][\w.\-]*)"
    r"(\[?[= ]?(?:<[^>]+>|\[[^\]]+\]|[A-Z][A-Z0-9_\-]*\b|\{[^}]+\}|"
    r"(?:int|uint|string|strings|duration|float|bool|value|list|path|file|num|n)\b))?")
SECTION_CMDS = re.compile(r"^\s*(?:[A-Za-z][\w -]{0,40}\s)?(?:sub-?)?(?:commands|actions)\s*:?\s*$", re.I)
SECTION_ANY = re.compile(r"^\S.*:\s*$|^[A-Z][A-Z ]+$")
CMD_LINE = re.compile(r"^\s{1,8}([a-z][a-z0-9_\-]{0,30}):?(?:,\s*[a-z][\w\-]*)*(?:\s{2,}|\t|\s*$)")


@dataclass
class Spec:
    opts: dict[str, bool] = field(default_factory=dict)   # name -> takes_arg
    subcmds: list[str] = field(default_factory=list)
    source: str = ""
    version: str = ""

    def has(self, opt: str) -> bool:
        return opt in self.opts

    def to_json(self) -> dict:
        return {"opts": self.opts, "subcmds": self.subcmds,
                "source": self.source, "version": self.version}

    @classmethod
    def from_json(cls, d: dict) -> "Spec":
        return cls(dict(d.get("opts", {})), list(d.get("subcmds", [])),
                   d.get("source", ""), d.get("version", ""))


def parse_help(text: str) -> Spec:
    spec = Spec()
    in_cmds = False
    for line in text.splitlines():
        line = re.sub(r"\x1b\[[0-9;]*m|.\x08", "", line)   # ANSI and man overstrike
        brace = re.match(r"^(?:usage:.*?\s|\s+)\{([\w,\-]+)\}", line, re.I)
        if brace:   # argparse subparsers: {list,show}
            for c in brace.group(1).split(","):
                if c and c not in spec.subcmds:
                    spec.subcmds.append(c)
            continue
        if SECTION_CMDS.match(line):
            in_cmds = True
            continue
        if in_cmds and line.strip() and SECTION_ANY.match(line):
            in_cmds = False
        if in_cmds:
            m = CMD_LINE.match(line)
            if m and m.group(1) not in spec.subcmds:
                spec.subcmds.append(m.group(1))
            continue
        m = OPT_LINE.match(line)
        if not m:
            continue
        head = m.group(1)
        for tm in OPT_TOKEN.finditer(head):
            name, arg = tm.group(1), tm.group(2)
            takes = bool(arg and arg.strip("[ =")) and not (arg or "").startswith("[")
            if name.startswith("--[no-]"):
                base = name[7:]
                spec.opts.setdefault("--" + base, takes)
                spec.opts.setdefault("--no-" + base, takes)
                continue
            name = name.rstrip(".,;")
            if "=" in name:
                name, takes = name.split("=", 1)[0], True
            if len(name) < 2 or name in ("--", "-"):
                continue
            spec.opts[name] = spec.opts.get(name, False) or takes
    if not spec.subcmds:
        # Help without a "Commands:" heading (git groups commands under prose
        # headings): take indented `name   description` lines, if there are many.
        loose = []
        for line in text.splitlines():
            m = re.match(r"^\s{2,8}([a-z][a-z0-9_\-]{1,30})\s{2,}\S", line)
            if m and m.group(1) not in loose:
                loose.append(m.group(1))
        if len(loose) >= 5:
            spec.subcmds = loose
    return spec


def _resolve(prog: str, path_env: str | None) -> str | None:
    if "/" in prog:
        return prog if os.access(prog, os.X_OK) else None
    for d in (path_env or os.environ.get("PATH", "")).split(":"):
        p = os.path.join(d or ".", prog)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def safe_to_run(path: str, name: str, proven_parser: bool) -> bool:
    base = os.path.basename(name)
    if base in NEVER_RUN or base.startswith("mkfs"):
        return False
    try:
        with open(path, "rb") as f:
            head = f.read(4)
            if head == b"\x7fELF":
                return True
            if head[:2] != b"#!":
                return False
            body = head + f.read(256 * 1024)
    except OSError:
        return False
    return proven_parser or bool(SCRIPT_PARSER_HINT.search(body))


def run_help(path: str, ctx: list[str], timeout: float = 2.0) -> str:
    env = dict(os.environ)
    env.update({"PAGER": "cat", "MANPAGER": "cat", "GIT_PAGER": "cat",
                "LESS": "-FRX", "NO_COLOR": "1", "TERM": "dumb",
                "LC_ALL": "C.UTF-8", "COLUMNS": "200"})
    best = ""
    for flag in ("--help", "-h"):
        try:
            p = subprocess.run([path, *ctx, flag], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=timeout, env=env,
                               start_new_session=True)
        except (subprocess.TimeoutExpired, OSError):
            continue
        out = (p.stdout + b"\n" + p.stderr).decode("utf-8", "replace")
        if _score(out) > _score(best):
            best = out
        if _score(best) >= 3:
            break
    return best


def _score(text: str) -> int:
    sp = parse_help(text)
    return len(sp.opts) + len(sp.subcmds)


def version_of(path: str, timeout: float = 1.5) -> str:
    for flag in ("--version", "-V", "version"):
        try:
            p = subprocess.run([path, flag], stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=timeout,
                               start_new_session=True,
                               env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"})
        except (subprocess.TimeoutExpired, OSError):
            continue
        out = (p.stdout or p.stderr).decode("utf-8", "replace").strip()
        if p.returncode == 0 and out and re.search(r"\d+\.\d+", out):
            return out.splitlines()[0][:120]
    return ""


def learn(prog: str, ctx: list[str], path_env: str | None = None,
          proven_parser: bool = False) -> Spec | None:
    path = _resolve(prog, path_env)
    if not path or not safe_to_run(path, prog, proven_parser):
        return None
    text = run_help(path, ctx)
    spec = parse_help(text)
    if not spec.opts and not spec.subcmds:
        return None
    spec.source = "help"
    return spec


def nearest(token: str, candidates, limit: int = 4) -> list[str]:
    """Candidates ranked by edit distance to token, then shared prefix."""
    scored = []
    for c in candidates:
        d = distance(token.lstrip("-"), c.lstrip("-"))
        if c.startswith(token) or token.startswith(c):
            d = min(d, 1)
        scored.append((d, -len(os.path.commonprefix([token, c])), c))
    scored.sort()
    return [c for d, _, c in scored[:limit] if d <= max(2, len(token) // 3)]


def distance(a: str, b: str) -> int:
    """Damerau-Levenshtein (optimal string alignment)."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = a[i - 1] != b[j - 1]
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[la][lb]


def unique_fix(bad: str, spec: Spec) -> str | None:
    """A correction only when exactly one option is an obvious match.

    Obvious = unique unambiguous long-option prefix (getopt semantics), or the
    single option at Damerau distance 1 with the same dash count, or a dash
    count slip (`-limit` for `--limit`, `--n` for `-n`).
    """
    if bad in spec.opts:
        return None
    name = bad.lstrip("-")
    # dash-count slip
    for cand in ("--" + name, "-" + name):
        if cand != bad and cand in spec.opts:
            return cand
    same_dash = [o for o in spec.opts if (o.startswith("--")) == bad.startswith("--")]
    if bad.startswith("--") and len(name) >= 3:
        pref = [o for o in same_dash if o.startswith(bad)]
        if len(pref) == 1:
            return pref[0]
    if len(name) >= 4:
        close = [o for o in same_dash if distance(o.lstrip("-"), name) == 1]
        if len(close) == 1:
            return close[0]
    return None
