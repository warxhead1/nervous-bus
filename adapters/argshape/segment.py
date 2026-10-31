"""Split a shell command line into simple commands, keeping byte spans.

Every word carries its [start, end) span in the original string so a
correction can replace exactly one token and leave the rest of the line,
quoting included, untouched. Stdlib only: this runs on every Bash call.

Scope: the POSIX/bash/zsh surface agents actually type. Words, quotes,
backslashes, $(...) / `...` / ${...} (kept inside the word, not descended
into), control operators, redirections, heredocs (body captured as the
segment's stdin), comments, env-assignment prefixes, and wrapper commands
(`timeout 5 rg`, `hearth-vault exec ... -- psql`, `docker exec ctr psql`).
Anything it cannot place it leaves alone: a correction is only ever made on
a word this parser fully understood.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

OPERATORS = ("&&", "||", "|&", ";;", "|", ";", "&", "(", ")")
REDIR_RE = re.compile(r"(\d*|&)(>>|>&|<&|<<<|<<-|<<|<>|>\||>|<)")
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until",
            "!", "{", "}", "time", "coproc"}
SKIP_COMPOUND = {"for", "case", "select", "function", "[[", "[", "test", "esac",
                 "in"}
# Wrappers whose own options end before the wrapped command. Value-taking
# options listed so `timeout -k 5 10 rg` skips "5".
WRAPPERS = {
    "nohup": set(), "command": set(), "builtin": set(), "exec": {"-a"},
    "nice": {"-n", "--adjustment"}, "ionice": {"-c", "-n", "-p", "-t"},
    "stdbuf": {"-i", "-o", "-e"}, "setsid": set(), "chrt": set(),
    "timeout": {"-k", "--kill-after", "-s", "--signal"},
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U"},
    "xargs": {"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s", "--max-args",
              "--max-procs", "--delimiter", "--arg-file", "--replace"},
    "watch": {"-n", "--interval", "-d"},
    "flock": {"-w", "--timeout", "-E", "-c"},
    "systemd-run": {"-u", "--unit", "-p", "--property", "--slice", "-E",
                    "--setenv", "--description"},
}
# Positional arguments a wrapper consumes before the wrapped command.
WRAPPER_POSITIONALS = {"timeout": 1, "chrt": 1, "flock": 1}
# Wrappers that hand off after a literal `--`.
DASHDASH_WRAPPERS = {"hearth-vault", "uv", "poetry", "pipx", "npx", "pnpm",
                     "cargo", "nix", "mmx", "systemd-run"}
DOCKER_EXEC_VALUE_OPTS = {"-e", "--env", "-u", "--user", "-w", "--workdir",
                          "--env-file", "--detach-keys"}


@dataclass
class Word:
    raw: str            # exact source text, quotes included
    value: str          # shell-unquoted value (best effort; $() kept verbatim)
    start: int
    end: int
    quoted: bool = False      # any quoting / escaping / substitution inside
    dynamic: bool = False     # contains $ expansion or backticks


@dataclass
class Segment:
    words: list[Word] = field(default_factory=list)   # argv after unwrapping
    assigns: list[Word] = field(default_factory=list)
    via: list[str] = field(default_factory=list)       # wrapper chain
    stdin_text: str | None = None
    stdin_span: tuple[int, int] | None = None
    start: int = 0
    end: int = 0
    redirect_targets: list[Word] = field(default_factory=list)

    @property
    def bin(self) -> str | None:
        return self.words[0].value if self.words else None

    @property
    def args(self) -> list[Word]:
        return self.words[1:]


@dataclass
class _Tok:
    kind: str           # "word" | "op" | "redir" | "nl" | "comment"
    text: str
    start: int
    end: int
    word: Word | None = None


def _scan_subst(s: str, i: int) -> int:
    """i points at the char after `$(`; return index just past the matching ')'."""
    depth = 1
    n = len(s)
    while i < n and depth:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == '"':
            i = _scan_dquote(s, i + 1)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    return i


def _scan_dquote(s: str, i: int) -> int:
    """i points after the opening quote; return index just past the closing one."""
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        if c == "$" and i + 1 < n and s[i + 1] == "(":
            i = _scan_subst(s, i + 2)
            continue
        if c == "`":
            j = s.find("`", i + 1)
            i = n if j < 0 else j + 1
            continue
        i += 1
    return n


def _unquote(raw: str) -> str:
    out = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "\\" and i + 1 < n:
            out.append(raw[i + 1])
            i += 2
        elif c == "'":
            j = raw.find("'", i + 1)
            j = n if j < 0 else j
            out.append(raw[i + 1:j])
            i = j + 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n and raw[j] != '"':
                if raw[j] == "\\" and j + 1 < n and raw[j + 1] in '"\\$`\n':
                    buf.append(raw[j + 1])
                    j += 2
                else:
                    buf.append(raw[j])
                    j += 1
            out.append("".join(buf))
            i = j + 1
        elif c == "$" and raw.startswith("$'", i):
            j = raw.find("'", i + 2)
            j = n if j < 0 else j
            out.append(raw[i + 2:j])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _tokenize(s: str) -> list[_Tok]:
    toks: list[_Tok] = []
    i, n = 0, len(s)
    pending_heredocs: list[tuple[str, bool, int]] = []  # (delim, strip_tabs, tok_index)
    while i < n:
        c = s[i]
        if c == "\n":
            toks.append(_Tok("nl", "\n", i, i + 1))
            i += 1
            # Heredoc bodies start on the line after their operator.
            for delim, strip, idx in pending_heredocs:
                body_start = i
                while i <= n:
                    j = s.find("\n", i)
                    line_end = n if j < 0 else j
                    line = s[i:line_end]
                    if (line.lstrip("\t") if strip else line) == delim:
                        toks[idx].word = Word(s[body_start:i], s[body_start:i],
                                              body_start, i)
                        i = line_end + 1 if j >= 0 else n
                        break
                    if j < 0:
                        toks[idx].word = Word(s[body_start:], s[body_start:],
                                              body_start, n)
                        i = n
                        break
                    i = j + 1
            pending_heredocs = []
            continue
        if c in " \t\r":
            i += 1
            continue
        if c == "\\" and i + 1 < n and s[i + 1] == "\n":
            i += 2
            continue
        if c == "#" and (i == 0 or s[i - 1] in " \t\n;&|()"):
            j = s.find("\n", i)
            j = n if j < 0 else j
            toks.append(_Tok("comment", s[i:j], i, j))
            i = j
            continue
        m = REDIR_RE.match(s, i) if (c in "<>&" or c.isdigit()) else None
        if m and (c in "<>" or m.group(2)):
            op = m.group(0)
            if not (c.isdigit() and not m.group(2)):
                toks.append(_Tok("redir", op, i, m.end()))
                i = m.end()
                if op.lstrip("0123456789&") in ("<<", "<<-"):
                    # Read the delimiter word now; register the body.
                    while i < n and s[i] in " \t":
                        i += 1
                    j = i
                    while j < n and s[j] not in " \t\n;&|<>()":
                        j += 1
                    delim = _unquote(s[i:j])
                    toks.append(_Tok("heredoc", s[i:j], i, j))
                    pending_heredocs.append((delim, op.endswith("-"), len(toks) - 1))
                    i = j
                continue
        op = next((o for o in OPERATORS if s.startswith(o, i)), None)
        if op:
            toks.append(_Tok("op", op, i, i + len(op)))
            i += len(op)
            continue
        # A word: run until unquoted whitespace or operator.
        start = i
        quoted = dynamic = False
        while i < n:
            c = s[i]
            if c in " \t\r\n;&|()<>":
                if c in "<>" and i == start:
                    break
                break
            if c == "\\":
                quoted = True
                i += 2
                continue
            if c == "'":
                quoted = True
                j = s.find("'", i + 1)
                i = n if j < 0 else j + 1
                continue
            if c == '"':
                quoted = True
                i = _scan_dquote(s, i + 1)
                continue
            if c == "$":
                dynamic = True
                if s.startswith("$(", i):
                    i = _scan_subst(s, i + 2)
                    continue
                if s.startswith("${", i):
                    j = s.find("}", i)
                    i = n if j < 0 else j + 1
                    continue
                if s.startswith("$'", i):
                    quoted = True
                    j = s.find("'", i + 2)
                    i = n if j < 0 else j + 1
                    continue
            if c == "`":
                dynamic = True
                j = s.find("`", i + 1)
                i = n if j < 0 else j + 1
                continue
            i += 1
        if i == start:      # a lone char no rule claimed; never stall
            i += 1
            continue
        raw = s[start:i]
        w = Word(raw, _unquote(raw), start, i, quoted, dynamic)
        toks.append(_Tok("word", raw, start, i, w))
    return toks


def _unwrap(seg: Segment) -> None:
    """Strip env assignments and wrapper commands so words[0] is the real bin."""
    ws = seg.words
    while ws:
        while ws and ASSIGN_RE.match(ws[0].value) and not ws[0].dynamic:
            seg.assigns.append(ws.pop(0))
        if not ws:
            break
        head = ws[0].value.rsplit("/", 1)[-1] if not ws[0].dynamic else ""
        if head in WRAPPERS:
            value_opts = WRAPPERS[head]
            j = 1
            while j < len(ws) and ws[j].value.startswith("-") and ws[j].value != "-":
                if ws[j].value == "--":
                    j += 1
                    break
                if ws[j].value in value_opts:
                    j += 2
                else:
                    j += 1
            if head == "env":
                while j < len(ws) and ASSIGN_RE.match(ws[j].value):
                    seg.assigns.append(ws[j])
                    j += 1
            j += WRAPPER_POSITIONALS.get(head, 0)
            if j >= len(ws):
                return
            seg.via.append(head)
            del ws[:j]
            continue
        if head in DASHDASH_WRAPPERS:
            idx = next((k for k, w in enumerate(ws) if w.value == "--"), None)
            if idx is not None and idx + 1 < len(ws):
                seg.via.append(" ".join(w.value for w in ws[:min(idx, 3)]))
                del ws[:idx + 1]
                continue
            return
        if head in ("docker", "podman") and len(ws) > 2 and ws[1].value == "exec":
            j = 2
            while j < len(ws) and ws[j].value.startswith("-"):
                j += 2 if ws[j].value in DOCKER_EXEC_VALUE_OPTS else 1
            if j + 1 < len(ws):
                seg.via.append(f"{head} exec {ws[j].value}")
                del ws[:j + 1]
                continue
            return
        return


def parse(command: str) -> list[Segment]:
    """Return the simple commands of `command`, in order."""
    toks = _tokenize(command)
    segs: list[Segment] = []
    cur = Segment(start=0)
    skip_until_sep = False

    def flush(end: int) -> None:
        nonlocal cur, skip_until_sep
        if cur.words and not skip_until_sep:
            cur.end = end
            _unwrap(cur)
            if cur.words:
                segs.append(cur)
        cur = Segment(start=end)
        skip_until_sep = False

    i = 0
    while i < len(toks):
        t = toks[i]
        if t.kind in ("op", "nl"):
            flush(t.start)
            cur.start = t.end
        elif t.kind == "comment":
            pass
        elif t.kind == "redir":
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if nxt is not None and nxt.kind == "heredoc":
                if nxt.word is not None:
                    cur.stdin_text = nxt.word.value
                    cur.stdin_span = (nxt.word.start, nxt.word.end)
                i += 1
            elif nxt is not None and nxt.kind == "word":
                if t.text.endswith("<<<"):
                    cur.stdin_text = nxt.word.value
                    cur.stdin_span = (nxt.word.start, nxt.word.end)
                else:
                    cur.redirect_targets.append(nxt.word)
                i += 1
        elif t.kind == "word":
            w = t.word
            if not cur.words and w.value in KEYWORDS and not w.quoted:
                pass
            elif not cur.words and w.value in SKIP_COMPOUND and not w.quoted:
                skip_until_sep = True
                cur.words.append(w)
            else:
                cur.words.append(w)
        i += 1
    flush(len(command))
    return segs


def has_optout(command: str) -> bool:
    return "argshape:off" in command
