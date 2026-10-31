#!/usr/bin/python3 -I
"""argshape — learn how each CLI and database wants to be called; correct, never block.

Hooks (Claude Code, Bash tool):
  argshape hook pre    PreToolUse          rewrite known-bad tokens (updatedInput), or advise
  argshape hook post   PostToolUse         settle corrections, learn session pairs, columns, shapes
  argshape hook fail   PostToolUseFailure  learn rejections + specs, tell the agent what is valid

Operator:
  argshape status                      mode, breaker, counts, recent corrections
  argshape mode [off|observe|advise|correct] [--bin NAME | --sql]
  argshape explain '<command>'         dry-run: segments + what pre would do
  argshape fixes [--all] | rejects | sigs [--top N] | columns | shapes
  argshape learn <bin> [subcmd ...]    learn a binary's --help now
  argshape demote <bin|conn> <bad> | promote <bin|conn> <bad>
  argshape breaker reset
  argshape install | uninstall         wire/unwire the hooks in ~/.claude/settings.json

Agents: append `# argshape:off` to a command to run it verbatim. If it follows
a correction, that correction is demoted to advice permanently.
Kill switch: ARGSHAPE_MODE=off, or `argshape mode off`.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import segment   # noqa: E402
import store     # noqa: E402

# `core` (and the regex-heavy modules it imports) loads lazily: the pre hook
# runs on every Bash call and must exit in interpreter-startup time when the
# command touches nothing argshape has a lesson about.
# SQL-looking text: a statement keyword followed by SQL structure, so English
# ("fix with X", "update the docs") does not take the slow post path.
SQLISH = re.compile(r"\bselect\b.+\bfrom\b|\bwith\s+\w+\s+as\s*\(|\bupdate\s+\w+\s+set\b|"
                    r"\bdelete\s+from\b|\binsert\s+into\b|\\d\+?\s", re.I | re.S)


def _core():
    import core
    return core

ERR_LOG_MAX = 256 * 1024


def _log_error() -> None:
    import traceback   # lazy: ~18 ms of import on the hot path otherwise
    try:
        os.makedirs(store.STATE_DIR, exist_ok=True)
        p = os.path.join(store.STATE_DIR, "errors.log")
        if os.path.exists(p) and os.path.getsize(p) > ERR_LOG_MAX:
            os.replace(p, p + ".1")
        with open(p, "a") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + traceback.format_exc() + "\n")
    except OSError:
        pass


def _out(event: str, **fields) -> None:
    fields = {k: v for k, v in fields.items() if v}
    if fields:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, **fields}}))


def hook(kind: str) -> int:
    try:
        ev = json.load(sys.stdin)
    except ValueError:
        return 0
    if ev.get("tool_name") != "Bash":
        return 0
    cfg = store.load_config()
    if cfg.get("mode") == "off":
        return 0
    tool_input = ev.get("tool_input") or {}
    command = tool_input.get("command") or ""
    if not command:
        return 0
    try:
        if kind == "pre":
            return _pre(ev, cfg, tool_input, command)
        if kind == "post" and not _post_has_work(ev, command):
            return 0
        core = _core()
        db = store.connect()
        with db:
            if kind == "post":
                notes = core.on_success(db, cfg, ev)
                event = "PostToolUse"
            else:
                notes = core.on_failure(db, cfg, ev)
                event = "PostToolUseFailure"
        if notes and cfg.get("mode") in ("advise", "correct"):
            _out(event, additionalContext="\n".join(notes)[:1500])
    except Exception:  # never break the agent's tool call
        _log_error()
    return 0


def _post_has_work(ev, command: str) -> bool:
    """A success only teaches something if it carries SQL output, settles a
    correction, or answers a pending rejection from this session."""
    if SQLISH.search(command):
        return True
    db = store.connect(create=False)
    if db is None:
        return False
    with db:
        return bool(db.execute(
            "SELECT 1 FROM applied WHERE tool_use_id=? AND outcome IS NULL "
            "UNION SELECT 1 FROM pending WHERE session=? LIMIT 1",
            (ev.get("tool_use_id") or "", ev.get("session_id") or "")).fetchone())


def _worth_planning(db, command: str) -> bool:
    """Cheap gate: does any segment's program, or any SQL, have a lesson?"""
    names = {r[0] for r in db.execute("SELECT DISTINCT name FROM rejects")}
    if not names and not db.execute("SELECT 1 FROM shapes LIMIT 1").fetchone():
        return False
    aliases = {r[0][6:] for r in db.execute("SELECT k FROM meta WHERE k LIKE 'alias:%'")}
    for seg in segment.parse(command):
        base = os.path.basename(seg.bin or "")
        if base in names or base in aliases:
            return True
    return bool(SQLISH.search(command)) and bool(
        db.execute("SELECT 1 FROM shapes UNION SELECT 1 FROM rejects WHERE kind='column' LIMIT 1").fetchone())


def _pre(ev, cfg, tool_input, command) -> int:
    if cfg.get("mode") == "observe":
        return 0
    db = store.connect(create=False)
    if db is None:
        return 0
    with db:
        session = ev.get("session_id") or ""
        if segment.has_optout(command):
            undone = _core().register_optout(db, session)
            if undone:
                what = ", ".join(f"`{f['bad']}` -> `{f['good']}`" for f in undone)
                _out("PreToolUse", additionalContext=f"argshape: opt-out noted; demoted {what} to advice-only.")
            return 0
        if not _worth_planning(db, command):
            return 0
        core = _core()
        result = core.plan(db, cfg, command, ev.get("cwd") or os.getcwd())
        if result["command"] != command:
            core.record_applied(db, session, ev.get("tool_use_id") or "", command,
                                result["command"], result["applied"])
            for a in result["applied"]:
                core.emit({"event": "correction", "bin": a.get("label", ""), "kind": a["kind"],
                           "bad": a["bad"], "good": a.get("render") or a["good"], "source": a["source"],
                           "session_id": session})
            ctx = ("argshape corrected this command before it ran (learned from earlier rejections): "
                   + "; ".join(result["notes"])
                   + ". Ran: " + result["command"][:400]
                   + "\nIf a correction is wrong, rerun your original with `# argshape:off` appended;"
                     " that demotes it.")
            _out("PreToolUse", updatedInput={**tool_input, "command": result["command"]},
                 additionalContext=ctx[:1500])
        elif result["notes"]:
            _out("PreToolUse", additionalContext="argshape: " + " | ".join(result["notes"])[:1200])
    return 0


# ---------------------------------------------------------------- CLI

def _table(rows, headers) -> None:
    rows = [[str(c) for c in r] for r in rows]
    w = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w[i]) for i, h in enumerate(headers)))
    for r in rows:
        print("  ".join(c[:60].ljust(min(w[i], 60)) for i, c in enumerate(r)))


def _ago(t) -> str:
    if not t:
        return "-"
    d = time.time() - float(t)
    for unit, s in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= s:
            return f"{int(d // s)}{unit}"
    return f"{int(d)}s"


def cmd_status(db, cfg) -> None:
    print(f"mode: {cfg['mode']}   sql: {cfg.get('sql')}   bins: {cfg.get('bins') or '{}'}")
    until = float(store.meta_get(db, "breaker_until", "0") or 0)
    print(f"breaker: {'OPEN until ' + time.strftime('%Y-%m-%d %H:%M', time.localtime(until)) + ' (advise-only)' if until > time.time() else 'closed'}")
    for t in ("bins", "specs", "rejects", "fixes", "sigs", "columns", "shapes", "applied"):
        n = db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print(f"  {t:8} {n}")
    print(f"db: {store.DB_PATH} ({os.path.getsize(store.DB_PATH) // 1024} KB)")
    rows = db.execute("SELECT ts, outcome, before, after FROM applied ORDER BY ts DESC LIMIT 8").fetchall()
    if rows:
        print("\nrecent corrections:")
        for ts, outcome, before, after in rows:
            print(f"  {_ago(ts):>4} {outcome or 'pending':12} {before[:70]!r}\n{'':19}-> {after[:70]!r}")


def cmd_mode(cfg, args) -> None:
    if not args:
        print(json.dumps({k: cfg[k] for k in ("mode", "sql", "bins")}, indent=2))
        return
    mode = args[0]
    if mode not in store.MODES:
        sys.exit(f"mode must be one of {store.MODES}")
    if "--bin" in args:
        name = args[args.index("--bin") + 1]
        cfg.setdefault("bins", {})[name] = mode
    elif "--sql" in args:
        cfg["sql"] = mode
    else:
        cfg["mode"] = mode
    store.save_config(cfg)
    print(f"ok: {' '.join(args)}")


def cmd_explain(db, cfg, command) -> None:
    for i, s in enumerate(segment.parse(command)):
        key, path = core.bin_identity(db, s.bin, os.getcwd())
        ctx, spec = core.context(db, key, s)
        print(f"[{i}] bin={s.bin} path={path} key={key} ctx={ctx!r} via={s.via} "
              f"spec={'%d opts/%d subcmds' % (len(spec.opts), len(spec.subcmds)) if spec else 'none'}")
        print(f"    argv={[w.value for w in s.words]}")
        if s.stdin_text:
            print(f"    stdin={s.stdin_text[:80]!r}")
    r = core.plan(db, {**cfg, "mode": "correct"}, command, os.getcwd())
    print("\nwould run:", r["command"] if r["command"] != command else "(unchanged)")
    for n in r["notes"]:
        print("  -", n)


def cmd_fixes(db, cfg, show_all) -> None:
    rows = db.execute("SELECT name, ctx, kind, bad, good, source, n, sessions, n_applied, n_ok, n_bad, demoted, scope "
                      "FROM fixes ORDER BY last DESC").fetchall()
    out = []
    for r in rows:
        f = dict(zip(("name", "ctx", "kind", "bad", "good", "source", "n", "sessions", "n_applied",
                      "n_ok", "n_bad", "demoted", "scope"), r))
        spec = store.get_spec(db, f["scope"], f["ctx"]) if f["kind"] == "flag" else None
        ok = core.fix_eligible(f, spec, cfg)
        if show_all or ok or f["demoted"]:
            out.append([f["name"], f["ctx"], f["kind"], f["bad"], f["good"], f["source"], f["n"],
                        f"{f['n_applied']}/{f['n_ok']}/{f['n_bad']}",
                        "DEMOTED" if f["demoted"] else ("auto" if ok else "advise")])
    _table(out, ["bin", "ctx", "kind", "bad", "good", "source", "seen", "applied/ok/bad", "state"])


def cmd_learn(db, args) -> None:
    import helpspec
    name = args[0]
    key, path = core.bin_identity(db, name, os.getcwd())
    if not path:
        sys.exit(f"{name}: not found on PATH")
    spec = helpspec.learn(path, args[1:], proven_parser=True)
    if not spec:
        sys.exit(f"{name}: no parseable --help")
    if not args[1:]:
        spec.version = helpspec.version_of(path)
        db.execute("INSERT OR REPLACE INTO bins VALUES (?,?,?,?,?,?)",
                   (key, os.path.basename(name), path, spec.version, None, time.time()))
    store.put_spec(db, key, " ".join(args[1:]), spec)
    print(f"{name} {' '.join(args[1:])}: {len(spec.opts)} options, {len(spec.subcmds)} subcommands"
          f"{', version ' + spec.version if spec.version else ''}")


def cmd_set_demoted(db, args, value: int) -> None:
    who, bad = args[0], args[1]
    n = db.execute("UPDATE fixes SET demoted=?, n_bad=CASE WHEN ?=0 THEN 0 ELSE n_bad END "
                   "WHERE (name=? OR scope=?) AND bad=?", (value, value, who, who, bad)).rowcount
    print(f"{'demoted' if value else 'promoted'} {n} fix(es)")


SETTINGS = os.path.join(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")), "settings.json")
HOOK_EVENTS = {"PreToolUse": "pre", "PostToolUse": "post", "PostToolUseFailure": "fail"}


def cmd_install(remove: bool) -> None:
    exe = os.path.expanduser("~/.local/bin/argshape")
    if sys.platform == "win32" or not os.path.exists(exe):
        # No POSIX link (Windows, or not installed): call this script through this interpreter.
        exe = f'"{sys.executable}" "{os.path.realpath(__file__)}"'
    with open(SETTINGS) as f:
        settings = json.load(f)
    hooks = settings.setdefault("hooks", {})
    changed = False
    for event, kind in HOOK_EVENTS.items():
        groups = hooks.setdefault(event, [])
        mine = [g for g in groups if any("argshape hook" in h.get("command", "") for h in g.get("hooks", []))]
        if remove:
            for g in mine:
                groups.remove(g)
                changed = True
        elif not mine:
            groups.append({"matcher": "Bash", "hooks": [
                {"type": "command", "command": f"{exe} hook {kind}", "timeout": 3 if kind == "pre" else 30}]})
            changed = True
    if not changed:
        print("no change")
        return
    backup = SETTINGS + time.strftime(".bak-argshape-%Y%m%d%H%M%S")
    with open(backup, "w") as f, open(SETTINGS) as src:
        f.write(src.read())
    tmp = SETTINGS + ".tmp-argshape"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    os.replace(tmp, SETTINGS)
    print(f"{'removed' if remove else 'installed'} argshape hooks (backup {backup})")


def main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    if cmd == "hook":
        return hook(args[0] if args else "pre")
    if cmd in ("install", "uninstall"):
        cmd_install(cmd == "uninstall")
        return 0
    global core
    core = _core()
    cfg = store.load_config()
    if cmd == "mode":
        cmd_mode(cfg, args)
        return 0
    db = store.connect()
    with db:
        if cmd == "status":
            cmd_status(db, cfg)
        elif cmd == "explain":
            cmd_explain(db, cfg, " ".join(args))
        elif cmd == "fixes":
            cmd_fixes(db, cfg, "--all" in args)
        elif cmd == "rejects":
            _table(db.execute("SELECT name, ctx, kind, token, n, last FROM rejects ORDER BY last DESC LIMIT 100")
                   .fetchall(), ["bin", "ctx", "kind", "token", "n", "last"])
        elif cmd == "sigs":
            top = int(args[args.index("--top") + 1]) if "--top" in args else 30
            _table(db.execute("SELECT name, ctx, err_class, n, sig FROM sigs ORDER BY n DESC LIMIT ?", (top,))
                   .fetchall(), ["bin", "ctx", "class", "n", "signature"])
        elif cmd == "columns":
            _table([(c[:8], r, len(json.loads(cols)), src, _ago(t)) for c, r, cols, src, t in
                    db.execute("SELECT conn, relation, cols, source, learned_at FROM columns")],
                   ["conn", "relation", "ncols", "source", "age"])
        elif cmd == "shapes":
            _table([(c[:8], *rest) for c, *rest in
                    db.execute("SELECT conn, col, family, variant, n FROM shapes")],
                   ["conn", "column", "family", "variant", "samples"])
        elif cmd == "learn":
            cmd_learn(db, args)
        elif cmd == "demote":
            cmd_set_demoted(db, args, 1)
        elif cmd == "promote":
            cmd_set_demoted(db, args, 0)
        elif cmd == "breaker" and args[:1] == ["reset"]:
            store.meta_set(db, "breaker_until", "0")
            print("breaker closed")
        else:
            print(__doc__)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
