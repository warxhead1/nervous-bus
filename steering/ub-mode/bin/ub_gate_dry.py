"""dryer with an exact similarity join and a whole-repo comparison for new code.

Runs inside the ub-gate virtualenv. Arguments are dryer's, plus:

  --against-repo   with --changed or --since: compare the changed forms
                   against every form in the project, not only each other.
  --since REF      forms in files changed since REF (committed, staged,
                   unstaged, or untracked), instead of dryer's --changed.
  --include-generated  also compare files marked generated ("Code generated
                   ... DO NOT EDIT." or "@generated" in the first 4 KB).
                   Skipped by default: a code generator repeats itself.

Output, exit codes, and .metrics/dry.edn are dryer's own.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

from dryer import cli
from dryer.discover import iter_source_files
from dryer.model import Duplicate, Span
from dryer.report import format_text, render_edn, write_metrics
from dryer.scan import _duplicate_key, _same_span, display_path, scan_files
from dryer.shape import jaccard


def _simjoin():
    """Load simjoin.py from this directory (the interpreter runs with -I)."""
    path = Path(__file__).resolve().with_name("simjoin.py")
    spec = importlib.util.spec_from_file_location("simjoin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATED = re.compile(rb"^// Code generated .* DO NOT EDIT\.$|@generated\b", re.M)


def _generated(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return bool(GENERATED.search(f.read(4096)))
    except OSError:
        return False


def _since_files(root: Path, ref: str) -> list[Path]:
    def git(*args: str) -> list[str]:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout
        return [line for line in out.splitlines() if line]

    top = Path(git("rev-parse", "--show-toplevel")[0])
    names = git("diff", "--name-only", "--diff-filter=AM", ref) + git("ls-files", "--others", "--exclude-standard")
    found = []
    for name in names:
        path = (top / name).resolve()
        if path.is_file() and path.is_relative_to(root.resolve()) and cli._tracked_source(path, root):
            found.append(path)
    return found


def _take_flag(args: list[str], flag: str, value: bool = False):
    if flag not in args:
        return args, None
    index = args.index(flag)
    if value:
        if index + 1 >= len(args):
            raise SystemExit(f"{flag} requires a value")
        return args[:index] + args[index + 2 :], args[index + 1]
    return args[:index] + args[index + 1 :], True


def main(argv: list[str]) -> int:
    argv, against_repo = _take_flag(argv, "--against-repo")
    argv, since = _take_flag(argv, "--since", value=True)
    argv, include_generated = _take_flag(argv, "--include-generated")
    options = cli.parse_args(argv)
    if options.action in {"help", "format"}:
        return cli.run(argv)
    root = options.project_root.resolve()
    try:
        if since:
            probe_files = _since_files(root, since)
        else:
            probe_files = cli.select_files(options)
    except cli.GitStatusError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    narrowed = bool(since or options.changed)
    if narrowed and against_repo:
        corpus = sorted({p.resolve() for p in iter_source_files([root])} | {p.resolve() for p in probe_files},
                        key=lambda p: p.as_posix())
    else:
        corpus = probe_files
    if not include_generated:
        corpus = [p for p in corpus if not _generated(p)]
        probe_files = [p for p in probe_files if not _generated(p)]
    if not corpus:
        print("No source files to analyze.")
        return 0
    entries, warnings = scan_files(corpus, root, options.min_lines, options.min_nodes)
    for warning in warnings:
        print(f"dryer: {warning}", file=sys.stderr)
    probe = None
    if narrowed and against_repo:
        wanted = {display_path(p, root) for p in probe_files}
        probe = {i for i, e in enumerate(entries) if e.file in wanted}
    found = []
    for i, j, score in _simjoin().similar_pairs(entries, options.threshold, jaccard, probe):
        left, right = entries[i], entries[j]
        if _same_span(left, right):
            continue
        found.append(Duplicate(score=score, language=left.language,
                               left=Span(left.file, left.start_line, left.end_line),
                               right=Span(right.file, right.start_line, right.end_line),
                               left_nodes=left.nodes, right_nodes=right.nodes))
    found.sort(key=_duplicate_key)
    metrics = write_metrics(found, root)
    print(render_edn(found) if options.format == "edn" else format_text(found), end="")
    print(f"Wrote {metrics}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
