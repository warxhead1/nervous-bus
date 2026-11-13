"""Static rewrite: a verify command piped into a reader reports its own status.

`cargo test 2>&1 | tail -20` exits with tail's status, so a failing run reads
as success to the agent and to every is_error-keyed hook. Prepending
`set -o pipefail; ` makes the pipeline carry the failure.

Only done when the rewrite cannot turn a green run red:
  * the line's last command list holds a pipeline whose FIRST stage is a
    verify-class command (verifyclass.classify);
  * every multi-stage pipeline on the line starts with a verify command
    (`set -o pipefail` is line-wide, so it would also change `git commit | tail`);
  * EVERY pipe stage after a first stage, in every pipeline on the line,
    reads all of its input (tail, tee, wc, sort, cat, grep/rg without
    -q/-m/-l and without a file operand). `set -o pipefail` is line-wide, so
    one `| head` anywhere would kill its producer with SIGPIPE (141);
  * nothing on the line sets or reads pipefail / PIPESTATUS, opts out, or
    masks the status (`|| true`);
  * the line has no `;`/newline/background follow-up after the verify
    pipeline (`cmd | tail; echo $?` is not safely rewritable).
Anything the scanner cannot place leaves the command alone.
"""
from __future__ import annotations

import os
import re

import segment
import verifyclass

PREFIX = "set -o pipefail; "
SEEN = re.compile(r"pipefail|PIPESTATUS|pipestatus")
PROCSUB = re.compile(r"[<>]\(")
BAD_LAST = {"done", "fi", "esac", "}", "!"}
START_KEYWORDS = {"then", "do", "else", "{", "time", "if", "elif", "while", "until"}
GREPS = {"grep", "egrep", "fgrep", "ugrep", "ug", "rg"}


def _stage_words(words):
    """Drop leading keywords and env assignments; None if the stage is not plain."""
    ws = list(words)
    while ws and not ws[0].quoted and ws[0].value in START_KEYWORDS:
        ws.pop(0)
    while ws and not ws[0].dynamic and segment.ASSIGN_RE.match(ws[0].value):
        ws.pop(0)
    return ws


def _verify_class(words) -> str:
    """Verify class of a first stage, looking through `hearth-vault exec -- `, `timeout`, `env` and the like."""
    seg = segment.Segment()
    seg.words = list(words)
    segment._unwrap(seg)
    if "xargs" in seg.via or "watch" in seg.via:
        return ""
    return verifyclass.classify(seg.words) or verifyclass.classify(words)


def _long_forbidden(name: str, forbidden) -> bool:
    return any(f == name or (len(name) >= 2 and f.startswith(name)) for f in forbidden)


def _scan_options(args, short_valued, long_valued, short_bad, long_bad):
    """Return (positionals, forbidden_seen, valued_flags_seen) for getopt-style args."""
    pos, bad, seen = [], False, set()
    i, only_pos = 0, False
    while i < len(args):
        w = args[i]
        v = w.value
        i += 1
        if w.dynamic and not w.quoted:
            return pos, True, seen
        if only_pos or v == "-" or not v.startswith("-"):
            pos.append(w)
            continue
        if v == "--":
            only_pos = True
            continue
        if v.startswith("--"):
            name, eq, _ = v[2:].partition("=")
            if _long_forbidden(name, long_bad):
                bad = True
            for lv in long_valued:
                if lv == name:
                    seen.add(lv)
                    if not eq:
                        i += 1
                    break
            else:
                # An abbreviation that could mean a valued option: cannot tell if it eats the next word.
                if any(lv.startswith(name) for lv in long_valued if lv != name) and not eq:
                    bad = True
            continue
        j = 1
        while j < len(v):
            c = v[j]
            if c in short_bad:
                bad = True
            if c in short_valued:
                seen.add(c)
                if j == len(v) - 1:
                    i += 1
                break
            j += 1
    return pos, bad, seen


def _grep_ok(name: str, args) -> bool:
    rg = name == "rg"
    if rg:
        short_valued = set("efmABCgtTjMEdr")
        short_bad = set("qmlhV")
        long_bad = {"quiet", "max-count", "files-with-matches", "files-without-match", "files",
                    "type-list", "help", "version"}
        long_valued = {"regexp", "file", "max-count", "after-context", "before-context", "context", "glob",
                       "iglob", "type", "type-not", "max-columns", "threads", "replace", "colors",
                       "encoding", "max-depth", "max-filesize", "sort", "sortr", "path-separator", "pre",
                       "pre-glob", "engine", "regex-size-limit", "dfa-size-limit", "context-separator",
                       "ignore-file", "type-add", "type-clear", "field-context-separator",
                       "field-match-separator", "hostname-bin", "hyperlink-format"}
    else:
        short_valued = set("efmABCdDX")
        short_bad = set("qmlLrRV")
        long_bad = {"quiet", "silent", "max-count", "files-with-matches", "files-without-match",
                    "recursive", "dereference-recursive", "help", "version"}
        long_valued = {"regexp", "file", "max-count", "after-context", "before-context", "context",
                       "directories", "devices", "include", "exclude", "exclude-from", "exclude-dir",
                       "label", "binary-files"}
    pos, bad, seen = _scan_options(args, short_valued, long_valued, short_bad, long_bad)
    if bad:
        return False
    if seen & {"e", "f", "regexp", "file"}:
        return not pos          # patterns given by option: any operand is a file
    return len(pos) <= 1


def _consumer_ok(words) -> bool:
    ws = list(words)
    while ws and not ws[0].dynamic and segment.ASSIGN_RE.match(ws[0].value):
        ws.pop(0)
    if len(ws) > 1 and ws[0].value in ("command", "builtin"):
        ws.pop(0)               # `command grep` bypasses the shell's grep function
    if not ws or ws[0].dynamic:
        return False
    name = os.path.basename(ws[0].value)
    args = ws[1:]
    if name == "tee":
        return all(not (a.dynamic and not a.quoted) for a in args)
    if name == "tail":
        pos, bad, _ = _scan_options(args, set("ncs"), {"lines", "bytes", "pid", "sleep-interval"},
                                    set("V"), {"help", "version"})
    elif name == "wc":
        pos, bad, _ = _scan_options(args, set(), {"files0-from"}, set("V"), {"files0-from", "help", "version"})
    elif name == "sort":
        pos, bad, _ = _scan_options(args, set("kotST"), {"key", "field-separator", "output", "buffer-size",
                                                          "temporary-directory", "parallel", "batch-size",
                                                          "compress-program", "random-source", "files0-from"},
                                    set("cCV"), {"check", "check-silent", "files0-from", "help", "version"})
    elif name == "cat":
        pos, bad, _ = _scan_options(args, set(), set(), set("V"), {"help", "version"})
    elif name in GREPS:
        return _grep_ok(name, args)
    else:
        return False
    return not bad and not pos


def rewrite(command: str) -> str | None:
    """The command with `set -o pipefail; ` prepended, or None to leave it alone."""
    if (not command or "|" not in command or segment.has_optout(command) or SEEN.search(command) or PROCSUB.search(command)
            or "\0" in command):
        return None
    toks = segment._tokenize(command)
    pipelines = []          # (stages, sep_after)
    stages = [[]]
    skip = False
    prev = None             # previous significant token text
    for t in toks:
        if skip and t.kind in ("word", "heredoc"):
            skip = False
            continue
        skip = False
        if t.kind == "comment" or t.kind == "heredoc":
            continue
        if t.kind == "redir":
            skip = True
            continue
        if t.kind == "word":
            if "|" in t.word.raw and ("$(" in t.word.raw or "`" in t.word.raw):
                return None     # a pipeline inside a substitution: not scanned
            stages[-1].append(t.word)
            prev = t.text
            continue
        if t.kind == "op" and t.text in ("|", "|&"):
            if prev in (")", None) or not stages[-1]:
                return None
            stages.append([])
            prev = t.text
            continue
        if t.kind == "op" and t.text in ("&", ";;"):
            return None
        if t.kind == "op" and t.text in ("(", ")"):
            prev = t.text
            continue
        # ;  &&  ||  newline
        sep = "nl" if t.kind == "nl" else t.text
        if any(not s for s in stages) and len(stages) > 1:
            return None
        pipelines.append((stages, sep))
        stages = [[]]
        prev = t.text
    if prev == "|" or (len(stages) > 1 and not stages[-1]):
        return None
    pipelines.append((stages, ";"))

    # A closing word (`done`, `fi`, `}`) is not a command: the list before it is the last one.
    pipelines = [(st, sep) for st, sep in pipelines
                 if any(st) and not (len(st) == 1 and len(st[0]) == 1 and st[0][0].value in BAD_LAST)]
    if not pipelines:
        return None
    # The final list: trailing pipelines joined by && / ||.
    start = len(pipelines) - 1
    while start > 0 and pipelines[start - 1][1] in ("&&", "||"):
        start -= 1
    final = pipelines[start:]
    verified = False
    for idx, (st, sep) in enumerate(pipelines):
        in_final = idx >= start
        # `|| true` and friends: the author wants the status gone.
        if idx > 0 and pipelines[idx - 1][1] == "||" and len(st) == 1:
            w = _stage_words(st[0])
            if w and w[0].value in ("true", ":") or (len(w) == 2 and w[0].value == "exit" and w[1].value == "0"):
                return None
        if len(st) < 2:
            continue
        first = _stage_words(st[0])
        if not first or (st[0] and st[0][-1].value in BAD_LAST):
            return None
        if any(s and s[-1].value in BAD_LAST for s in st[:-1]):
            return None
        if not all(_consumer_ok(s) for s in st[1:]):
            return None
        if _verify_class(first):
            verified = verified or in_final
        else:
            return None         # a non-verify pipe: line-wide pipefail would change its status too
    if not verified:
        return None
    if not any(len(st) > 1 for st, _ in final):
        return None
    return PREFIX + command
